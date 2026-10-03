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

# أنماط دقيقة جداً للبحث عن المعرفات داخل الروابط ومحتوى الصفحات
G_PAGE_REVIEW_PATTERN = re.compile(r"g\.page/r/([A-Za-z0-9_-]+)(?:/review)?", re.IGNORECASE)
PLACE_ID_PATTERN = re.compile(r"(ChIJ[A-Za-z0-9_-]{10,})")
CID_PATTERN = re.compile(r"0x[0-9a-f]{1,16}:0x[0-9a-f]{1,16}", re.IGNORECASE)

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

async def resolve_google_link(url: str) -> str:
    current_url = validate_google_url(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(12.0, connect=5.0),
        headers=headers
    ) as client:
        try:
            response = await client.get(current_url)
            # نجمع عنوان URL النهائي مع محتوى الصفحة النصي للبحث بداخلهم
            page_content = response.text
            final_url = str(response.url)
            return final_url + " " + page_content
        except Exception:
            return current_url

@app.post("/api/resolve-share-link")
async def resolve_share_link(request: ResolveRequest) -> dict[str, object]:
    input_url = request.url.strip()
    
    # 1. فحص فوري إذا كان المدخل أصلاً رابط g.page مباشر
    direct_g_page = G_PAGE_REVIEW_PATTERN.search(input_url)
    if direct_g_page:
        code = direct_g_page.group(1)
        return {"review_url": f"https://g.page/r/{code}/review"}

    # 2. تتبع الرابط وفحص محتوى الصفحة بالكامل
    full_text_data = await resolve_google_link(input_url)

    # البحث عن رابط g.page داخل محتوى الصفحة أو التحويلات
    g_page_match = G_PAGE_REVIEW_PATTERN.search(full_text_data)
    if g_page_match:
        code = g_page_match.group(1)
        return {"review_url": f"https://g.page/r/{code}/review"}

    # البحث عن Place ID (ChIJ...) داخل محتوى الصفحة
    place_match = PLACE_ID_PATTERN.search(full_text_data)
    if place_match:
        place_id = place_match.group(1)
        return {"review_url": f"https://search.google.com/local/writereview?placeid={place_id}"}

    raise HTTPException(
        status_code=400,
        detail="تعذر استخراج رابط التقييم المباشر. تأكد من أن الرابط يتبع لمتجر حقيقي على خرائط جوجل."
    )

app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
