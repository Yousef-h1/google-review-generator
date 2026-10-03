from __future__ import annotations

import os
import re
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

WEB_DIR = __import__("pathlib").Path(__file__).resolve().parent
MAX_REDIRECTS = 8
GOOGLE_HOSTS = {"share.google", "g.page", "maps.app.goo.gl", "google.com", "www.google.com"}
GOOGLE_SHORT_LINK_HOSTS = {"share.google", "maps.app.goo.gl"}

# أنماط استخراج معرفات g.page أو Place ID أو الرموز المختصرة
G_PAGE_REVIEW_PATTERN = re.compile(r"/r/([A-Za-z0-9_-]+)(?:/review)?", re.IGNORECASE)
PLACE_ID_PATTERN = re.compile(r"!1s(ChIJ[A-Za-z0-9_-]{10,})")

app = FastAPI(title="Google Review Link Generator", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ResolveRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)

def validate_google_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("الرجاء إدخال رابط صحيح يبدأ بـ https://")
    return value

async def follow_google_redirects(url: str) -> str:
    current_url = validate_google_url(url)
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"}
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            # فحص مباشر إذا كان الرابط الحالي يحتوي أصلاً على كود g.page
            if "g.page/r/" in current_url:
                return current_url
                
            async with client.stream("GET", current_url) as response:
                if response.status_code not in {301, 302, 303, 307, 308}:
                    return current_url
                location = response.headers.get("location")
                if not location:
                    return current_url
                current_url = urljoin(current_url, location)
                if "g.page/r/" in current_url:
                    return current_url
    return current_url

@app.post("/api/resolve-share-link")
async def resolve_share_link(request: ResolveRequest) -> dict[str, object]:
    input_url = request.url.strip()
    try:
        resolved_url = await follow_google_redirects(input_url)
    except Exception:
        resolved_url = input_url

    # البحث عن كود g.page/r/XXXXX في الرابط الأصلي أو الموجه
    match = G_PAGE_REVIEW_PATTERN.search(resolved_url) or G_PAGE_REVIEW_PATTERN.search(input_url)
    if match:
        code = match.group(1)
        clean_review_url = f"https://g.page/r/{code}/review"
        return {
            "review_url": clean_review_url
        }

    # البحث عن Place ID واستخراج رابط writereview الصحيح
    place_match = PLACE_ID_PATTERN.search(resolved_url) or PLACE_ID_PATTERN.search(input_url)
    if place_match:
        place_id = place_match.group(1)
        return {
            "review_url": f"https://search.google.com/local/writereview?placeid={place_id}"
        }

    raise HTTPException(
        status_code=400,
        detail="تعذر استخراج رابط التقييم المباشر. تأكد من إدخال رابط خرائط صحيح أو رابط g.page مباشر."
    )

app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
