from __future__ import annotations

import os
import re
import base64
from urllib.parse import parse_qs, unquote, urljoin, urlparse
import urllib.parse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


WEB_DIR = __import__("pathlib").Path(__file__).resolve().parent
MAX_REDIRECTS = 8
GOOGLE_HOSTS = {"share.google", "g.page", "maps.app.goo.gl", "google.com", "www.google.com"}
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
    return normalized in GOOGLE_HOSTS or normalized.endswith(".google.com") or normalized.endswith(".google")


def validate_google_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or not is_google_host(parsed.hostname):
        raise ValueError("أدخل رابطاً صحيحاً خاصاً بخرائط Google.")
    return value


def extract_place_id(value: str) -> str | None:
    parsed = urlparse(value)
    for key in ("query_place_id", "place_id", "destination_place_id", "origin_place_id"):
        place_ids = parse_qs(parsed.query).get(key)
        if place_ids and place_ids[0]:
            return place_ids[0]

    match = PLACE_ID_PATTERN.search(f"{parsed.path} {parsed.query} {parsed.fragment}")
    return match.group(1) if match else None


def extract_maps_cid(value: str) -> str | None:
    parsed = urlparse(value)
    match = MAPS_CID_PATTERN.search(f"{parsed.path} {parsed.query} {parsed.fragment}")
    return match.group(1) if match else None


def make_review_url(place_id: str) -> str:
    return f"https://search.google.com/local/writereview?placeid={place_id}"


def extract_g_page_review_id(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.hostname and "g.page" in parsed.hostname:
        match = G_PAGE_REVIEW_PATTERN.fullmatch(parsed.path)
        if match:
            return match.group(1)
    return None


def make_g_page_review_url(review_id: str) -> str:
    return f"https://g.page/r/{review_id}/review"


async def follow_google_redirects(url: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    current_url = validate_google_url(url)
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(12.0, connect=5.0),
        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"},
        transport=transport,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", current_url) as response:
                if response.status_code not in {301, 302, 303, 307, 308}:
                    return current_url
                location = response.headers.get("location")
                if not location:
                    return current_url
                current_url = validate_google_url(urljoin(current_url, location))
    return current_url


@app.post("/api/resolve-share-link")
async def resolve_share_link(request: ResolveRequest) -> dict[str, object]:
    try:
        resolved_url = await follow_google_redirects(request.url.strip())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=502, detail="تعذر الاتصال بـ Google لفك الرابط.") from error

    # 1. التحقق من رابط g.page مباشر
    review_id = extract_g_page_review_id(resolved_url) or extract_g_page_review_id(request.url.strip())
    if review_id:
        return {
            "resolved_url": resolved_url,
            "review_url": make_g_page_review_url(review_id),
            "places": [],
        }

    # 2. استخراج Place ID إن وجد مباشرة
    place_id = extract_place_id(resolved_url) or extract_place_id(request.url.strip())
    if place_id:
        return {
            "resolved_url": resolved_url,
            "review_url": make_review_url(place_id),
            "places": [],
        }

    # 3. استخراج اسم المكان من مسار الرابط الموجه للروابط القصيرة
    parsed = urlparse(resolved_url)
    path_segments = parsed.path.split("/")
    place_name = ""
    for i, seg in enumerate(path_segments):
        if seg == "place" and i + 1 < len(path_segments):
            place_name = unquote(path_segments[i + 1]).replace("+", " ")
            break

    if place_name:
        return {
            "resolved_url": resolved_url,
            "review_url": f"https://search.google.com/local/writereview?placeid=&q={urllib.parse.quote(place_name)}",
            "places": [],
        }

    # 4. حل افتراضي آمن للروابط المبهمة لضمان عدم تعطل الأداة أبداً
    return {
        "resolved_url": resolved_url,
        "review_url": f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(resolved_url)}",
        "places": [],
    }


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
