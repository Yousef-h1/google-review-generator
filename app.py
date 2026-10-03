from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

WEB_DIR = __import__("pathlib").Path(__file__).resolve().parent
MAX_REDIRECTS = 10

# أنماط دقيقة لاستخراج الرموز ومعرفات التقييم
G_PAGE_PATTERN = re.compile(r"g\.page/r/([A-Za-z0-9_-]+)(?:/review)?", re.IGNORECASE)
PLACE_ID_PATTERN = re.compile(r"(ChIJ[A-Za-z0-9_-]{10,})")

app = FastAPI(title="Google Review Direct Linker", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ResolveRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)

async def resolve_map_link(url: str) -> str:
    current_url = url.strip()
    # محاكاة متصفح جوال حقيقي لتجنب حظر جوجل
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
        "Accept-Language": "ar,en;q=0.9"
    }
    
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers=headers
    ) as client:
        for _ in range(MAX_REDIRECTS):
            # إذا ظهر رابط g.page أثناء التتبع، نعتبره وصل للهدف فوراً
            if "g.page/r/" in current_url:
                return current_url
                
            try:
                response = await client.get(current_url)
                # فحص ما إذا كان الرابط النهائي يحتوي على محتوى أو تحويل
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        break
                    current_url = urljoin(current_url, location)
                    continue
                
                # إذا تم تحميل الصفحة بنجاح، ندمج العنوان مع النص للبحث عن المعرفات بداخلها
                return current_url + " " + response.text
            except Exception:
                break
                
    return current_url

@app.post("/api/resolve-share-link")
async def resolve_share_link(request: ResolveRequest) -> dict[str, object]:
    input_url = request.url.strip()
    
    # 1. التتبع الذكي عبر بايثون
    full_trace_data = await resolve_map_link(input_url)
    
    # 2. البحث عن كود g.page مباشر
    g_match = G_PAGE_PATTERN.search(full_trace_data)
    if g_match:
        code = g_match.group(1)
        return {"review_url": f"https://g.page/r/{code}/review"}
        
    # 3. البحث عن Place ID لتوليد رابط writereview
    place_match = PLACE_ID_PATTERN.search(full_trace_data)
    if place_match:
        place_id = place_match.group(1)
        return {"review_url": f"https://search.google.com/local/writereview?placeid={place_id}"}
        
    raise HTTPException(
        status_code=400,
        detail="تعذر استخراج رابط التقييم المباشر. تأكد من صحة الرابط."
    )

app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
