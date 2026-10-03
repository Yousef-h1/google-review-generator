from __future__ import annotations

import os
import re
import base64
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


WEB_DIR = __import__("pathlib").Path(__file__).resolve().parent
MAX_REDIRECTS = 8
GOOGLE_HOSTS = {"share.google", "g.page", "maps.app.goo.gl"}
GOOGLE_SHORT_LINK_HOSTS = {"share.google", "maps.app.goo.gl"}
PLACE_ID_PATTERN = re.compile(r"!1s(ChIJ[A-Za-z0-9_-]{10,})")
MAPS_CID_PATTERN = re.compile(r"!1s(0x[0-9a-f]{1,16}:0x[0-9a-f]{1,16})", re.IGNORECASE)
G_PAGE_REVIEW_PATTERN = re.compile(r"^/r/([^/]+)(?:/review)?/?$", re.IGNORECASE)

app = FastAPI(title="Google Review Link Generator", docs_url=None, redoc_url=None)

# تفعيل دعم CORS للسماح لموقع ووردبريس بالاتصال بالخادم
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ResolveRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


def is_google_host(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    return normalized in GOOGLE_HOSTS or normalized == "google.com" or normalized.endswith(".google.com")


def validate_google_url(value: str, *, short_link_only: bool = False) -> str:
    parsed = urlparse(value)
    if short_link_only and (parsed.scheme != "https" or parsed.hostname not in GOOGLE_SHORT_LINK_HOSTS):
        raise ValueError("أدخل رابطاً يبدأ بـ https://share.google/ أو https://maps.app.goo.gl/.")
    if parsed.scheme != "https" or not parsed.hostname or not is_google_host(parsed.hostname):
        raise ValueError("رفضنا تحويل الرابط لأنه خرج عن نطاقات Google الآمنة.")
    return value


def extract_place_id(value: str) -> str | None:
    parsed = urlparse(value)
    for key in ("query_place_id", "place_id", "destination_place_id", "origin_place_id"):
        place_ids = parse_qs(parsed.query).get(key)
        if place_ids and place_ids[0]:
            return place_ids[0]

    query = parse_qs(parsed.query)
    for key in ("q", "query"):
        for match in re.finditer(r"place_id\s*:\s*([A-Za-z0-9_-]{10,})", " ".join(query.get(key, [])), re.I):
            return match.group(1)

    match = PLACE_ID_PATTERN.search(f"{parsed.path} {parsed.query} {parsed.fragment}")
    return match.group(1) if match else None


def extract_maps_cid(value: str) -> str | None:
    parsed = urlparse(value)
    match = MAPS_CID_PATTERN.search(f"{parsed.path} {parsed.query} {parsed.fragment}")
    return match.group(1) if match else None


def extract_maps_context(value: str) -> tuple[str, str, str, str, str | None]:
    parsed = urlparse(value)
    place_match = re.search(r"/maps/place/([^/@]+)", parsed.path, re.IGNORECASE)
    name = unquote(place_match.group(1)).replace("+", " ").strip() if place_match else ""
    coordinate_match = re.search(r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", parsed.path)
    latitude, longitude = coordinate_match.groups() if coordinate_match else ("0", "0")
    query = parse_qs(parsed.query)
    language = query.get("hl", ["ar"])[0]
    region = query.get("gl", [None])[0]
    return name, latitude, longitude, language, region


async def resolve_place_id_from_cid(
    value: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    cid = extract_maps_cid(value)
    if not cid:
        return None

    name, latitude, longitude, language, region = extract_maps_context(value)
    encoded_name = base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
    pb = (
        f"!1m15!1s{cid}!2z{encoded_name}!3m12!1m3!1d57276.13!2d{longitude}!3d{latitude}"
        "!2m3!1f0.0!2f0.0!3f0.0!3m2!1i1024!2i768!4f13.1"
    )
    params: dict[str, str] = {"authuser": "0", "hl": language, "q": name, "pb": pb}
    if region:
        params["gl"] = region
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12.0, connect=5.0),
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
            "Referer": "https://www.google.com/maps/",
        },
        transport=transport,
    ) as client:
        response = await client.get("https://www.google.com/maps/preview/place", params=params)
    if response.is_error:
        raise ValueError("تعذر استعلام Google Maps عن المعرّف المرتبط بـ CID.")

    match = re.search(r"\bChIJ[A-Za-z0-9_-]{10,}", response.text)
    return match.group(0) if match else None


def make_review_url(place_id: str) -> str:
    return f"https://search.google.com/local/writereview?placeid={place_id}"


def extract_g_page_review_id(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.hostname != "g.page":
        return None
    match = G_PAGE_REVIEW_PATTERN.fullmatch(parsed.path)
    return match.group(1) if match else None


def make_g_page_review_url(review_id: str) -> str:
    return f"https://g.page/r/{review_id}/review"


async def follow_google_redirects(url: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    current_url = validate_google_url(url, short_link_only=True)
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(12.0, connect=5.0),
        headers={"User-Agent": "Mozilla/5.0 (compatible; ReviewLinkTool/1.0)", "Range": "bytes=0-0"},
        transport=transport,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", current_url) as response:
                if response.status_code not in {301, 302, 303, 307, 308}:
                    if response.status_code >= 400:
                        raise ValueError("لم يتمكن Google من فتح الرابط. تحقق من صحته وحاول مجدداً.")
                    return current_url
                location = response.headers.get("location")
                if not location:
                    raise ValueError("أعاد Google تحويلاً بلا وجهة صالحة.")
                current_url = validate_google_url(urljoin(current_url, location))
    raise ValueError("تجاوز الرابط الحد المسموح من التحويلات.")


@app.post("/api/resolve-share-link")
async def resolve_share_link(request: ResolveRequest) -> dict[str, object]:
    try:
        resolved_url = await follow_google_redirects(request.url)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=502, detail="تعذر الاتصال بـ Google لفك الرابط. تحقق من الاتصال وحاول مجدداً.") from error

    review_id = extract_g_page_review_id(resolved_url)
    place_id = extract_place_id(resolved_url)
    if review_id:
        return {
            "resolved_url": resolved_url,
            "review_id": review_id,
            "place_id": None,
            "review_url": make_g_page_review_url(review_id),
            "places": [],
        }
    if place_id:
        return {
            "resolved_url": resolved_url,
            "review_id": None,
            "place_id": place_id,
            "review_url": make_review_url(place_id),
            "places": [],
        }

    if extract_maps_cid(resolved_url):
        try:
            place_id = await resolve_place_id_from_cid(resolved_url)
        except (httpx.RequestError, ValueError):
            place_id = None
        if place_id:
            return {
                "resolved_url": resolved_url,
                "review_id": None,
                "place_id": place_id,
                "review_url": make_review_url(place_id),
                "places": [],
            }

    # تم الاستغناء نهائياً عن مفتاح Google Places API وتوجيه المستخدم لوضع رابط مباشر
    raise HTTPException(
        status_code=422,
        detail="يرجى استخدام رابط خرائط جوجل المباشر أو رابط g.page الذي يحتوي على معرّف المكان مباشرة، لضمان عمل الأداة مجاناً وبدون الحاجة لأي مفتاح API.",
    )


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
