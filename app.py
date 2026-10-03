from __future__ import annotations

import os
import re
import base64
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


WEB_DIR = __import__("pathlib").Path(__file__).resolve().parent
MAX_REDIRECTS = 8
GOOGLE_HOSTS = {"share.google", "g.page", "maps.app.goo.gl"}
GOOGLE_SHORT_LINK_HOSTS = {"share.google", "maps.app.goo.gl"}
PLACE_ID_PATTERN = re.compile(r"!1s(ChIJ[A-Za-z0-9_-]{10,})")
MAPS_CID_PATTERN = re.compile(r"!1s(0x[0-9a-f]{1,16}:0x[0-9a-f]{1,16})", re.IGNORECASE)
G_PAGE_REVIEW_PATTERN = re.compile(r"^/r/([^/]+)(?:/review)?/?$", re.IGNORECASE)

app = FastAPI(title="Google Review Link Generator", docs_url=None, redoc_url=None)


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


def extract_business_query(value: str) -> str | None:
    parsed = urlparse(value)
    if not parsed.hostname or not is_google_host(parsed.hostname):
        return None
    query = parse_qs(parsed.query).get("q", [""])[0].strip()
    return query or None


async def search_google_places(
    query: str,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, str]]:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12.0, connect=5.0),
        transport=transport,
    ) as client:
        response = await client.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress",
            },
            json={"textQuery": query},
        )
    if response.is_error:
        raise ValueError("تعذر البحث عن النشاط عبر Google Places. تحقق من إعداد الخادم وصلاحيات API.")

    places = response.json().get("places", [])
    return [
        {
            "place_id": place["id"],
            "name": place.get("displayName", {}).get("text", "نشاط تجاري"),
            "address": place.get("formattedAddress", ""),
        }
        for place in places
        if isinstance(place, dict) and place.get("id")
    ]


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

    query = extract_business_query(resolved_url)
    if not query:
        raise HTTPException(status_code=422, detail="لم يتضمن الرابط اسماً أو معرّفاً يمكن البحث به.")

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="رابط المشاركة من بحث Google لا يحتوي Place ID. من الهاتف افتح النشاط في تطبيق خرائط Google ثم اختر مشاركة > نسخ الرابط واستخدم رابط maps.app.goo.gl؛ أو اضبط مفتاح Places API مرة واحدة في متغير الخادم GOOGLE_MAPS_API_KEY.",
        )

    try:
        places = await search_google_places(query, api_key)
    except httpx.RequestError as error:
        raise HTTPException(status_code=502, detail="تعذر البحث عن النشاط عبر Google Places.") from error
    except ValueError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if not places:
        raise HTTPException(status_code=404, detail="لم يعثر Google Places على نشاط مطابق للرابط.")

    selected_place = places[0] if len(places) == 1 else None
    return {
        "resolved_url": resolved_url,
        "review_id": None,
        "place_id": selected_place["place_id"] if selected_place else None,
        "review_url": make_review_url(selected_place["place_id"]) if selected_place else None,
        "places": places,
    }


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")