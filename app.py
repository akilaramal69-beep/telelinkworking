import os
import asyncio
import time
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from plugins.config import Config
from utils.shared import WEBAPP_PROGRESS, bot_client

# Initialize FastAPI
app = FastAPI(title="URL Uploader API")

# Add CORS Middleware for Telegram WebApp compatibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the static files from 'web' folder
app.mount("/web", StaticFiles(directory="web"), name="web")

# Runtime flags
app.is_ready = False
app.is_shutting_down = False
app.bot_loop = None

# Global cache for index.html
_INDEX_HTML_CACHE = None

async def prune_progress_task():
    """Background task to keep memory low by pruning old progress data."""
    while True:
        try:
            now = time.time()
            # Remove entries that haven't been updated for 1 hour
            to_del = [uid for uid, info in WEBAPP_PROGRESS.items() 
                      if now - info.get("_last_update", now) > 3600]
            for uid in to_del:
                del WEBAPP_PROGRESS[uid]
        except Exception:
            pass
        await asyncio.sleep(600) # Check every 10 mins

@app.get("/", response_class=HTMLResponse)
async def index():
    global _INDEX_HTML_CACHE
    if app.is_shutting_down:
        raise HTTPException(status_code=503, detail="🔄 Bot is shutting down…")
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="⏳ Bot is starting…")

    if _INDEX_HTML_CACHE:
        return _INDEX_HTML_CACHE

    try:
        html_path = os.path.join("web", "index.html")
        if not os.path.exists(html_path):
            raise HTTPException(status_code=404, detail="404 - Web assets missing")
            
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
            # Inject Block ID directly into HTML
            content = content.replace("{{ADSGRAM_BLOCK_ID}}", Config.ADSGRAM_BLOCK_ID)
            _INDEX_HTML_CACHE = content
            return content
    except Exception as e:
        Config.LOGGER.error(f"Error serving index: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.get("/{path:path}")
async def serve_static(path: str):
    # Try serving from web directory
    file_path = os.path.join("web", path)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return FileResponse(file_path)
    # Default fallback to index for SPA-like behavior if needed, 
    # but here we just return 404 if not found
    raise HTTPException(status_code=404)

@app.get('/api/config')
async def api_config():
    """Return public configuration values to the frontend."""
    return {
        "adsgram_block_id": Config.ADSGRAM_BLOCK_ID
    }

class FormatsRequest(BaseModel):
    url: str

@app.post("/api/formats")
async def api_formats(req: FormatsRequest):
    """Endpoint for MiniApp to extract video qualities without uploading."""
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    url = req.url
    if not url:
        raise HTTPException(status_code=400, detail="No URL provided")

    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        raise HTTPException(status_code=403, detail="YouTube downloading not allowed.")

    from plugins.helper.upload import fetch_ytdlp_formats
    
    try:
        # fetch_ytdlp_formats is async and IO-bound (using executor internally for yt-dlp)
        # We can await it directly. No need for run_coroutine_threadsafe unless we touch bot client.
        res = await fetch_ytdlp_formats(url)
        return res
    except Exception as e:
        Config.LOGGER.exception(f"API Formats Error for {url}")
        raise HTTPException(status_code=500, detail=str(e))

class DownloadRequest(BaseModel):
    url: str
    chat_id: int
    format_id: str = None
    mode: str = "media"
    filename: str = None

@app.post("/api/download")
async def api_download(req: DownloadRequest):
    """Triggered when user clicks 'Beam to Chat' in the MiniApp"""
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    url = req.url
    if not url or not req.chat_id:
        raise HTTPException(status_code=400, detail="URL or chat_id missing.")

    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        raise HTTPException(status_code=403, detail="YouTube downloading not allowed.")

    from plugins.commands import trigger_webapp_download
    
    try:
        if app.bot_loop:
            asyncio.run_coroutine_threadsafe(
                trigger_webapp_download(req.chat_id, url, req.format_id, req.mode, req.filename), 
                app.bot_loop
            )
        else:
            # Fallback if loop isn't captured yet
            asyncio.create_task(trigger_webapp_download(req.chat_id, url, req.format_id, req.mode, req.filename))
        return {"status": "queued"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class CancelRequest(BaseModel):
    user_id: int

@app.post("/api/cancel")
async def api_cancel(req: CancelRequest):
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    user_id = req.user_id
    from plugins.commands import ACTIVE_TASKS
    task_info = ACTIVE_TASKS.get(user_id)
    if not task_info:
        raise HTTPException(status_code=404, detail="No active process to cancel.")

    task, cancel_ref = task_info
    cancel_ref[0] = True
    
    if app.bot_loop:
        app.bot_loop.call_soon_threadsafe(task.cancel)
    else:
        task.cancel()

    return {"status": "cancelled"}

@app.get("/api/progress")
async def api_progress(user_id: int):
    """Endpoint for MiniApp to poll live download/upload progress."""
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="Bot is not ready")

    progress_data = WEBAPP_PROGRESS.get(user_id)
    
    if progress_data:
        return progress_data
    else:
        return {"action": "idle", "percentage": 0}

# ── Sniffer API Compatibility (link-api) ──────────────────────────────────────
# See: https://github.com/akilaramal69-beep/link-api

@app.get("/api/link")
async def link_api_info():
    """Link-API discovery — returns available endpoints."""
    return {
        "message": "Direct Link Grabber API (integrated) — IDM-style",
        "endpoints": {
            "GET /grab?url=<URL>": "Grab links from any video URL",
            "POST /grab": '{"url": "...", "use_browser": true, "timeout": 25}',
            "POST /extract": '{"url": "..."} — yt-dlp compatible formats',
            "GET /health": "API status check",
        },
    }


class LinkRequest(BaseModel):
    """Request body for POST /grab — link-api compatibility."""
    url: str
    use_browser: bool = True  # False = force yt-dlp only
    timeout: int = 25  # seconds


@app.get("/grab")
async def grab_get(
    url: str = Query(..., description="Any video page URL"),
    use_browser: bool = Query(True, description="Use headless browser interception"),
    timeout: int = Query(25, description="Timeout in seconds"),
):
    """Extract direct media links from any video URL (link-api compatible)."""
    try:
        from plugins.helper.extractor import extract_links
        result = await extract_links(url, use_browser=use_browser, timeout=timeout)
        if not result.get("links"):
            raise HTTPException(status_code=400, detail=f"No media links found for: {url}")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Extraction error: {str(e)}")


@app.post("/grab")
async def grab_post(req: LinkRequest):
    """Extract direct media links from any video URL (POST — link-api compatible)."""
    try:
        from plugins.helper.extractor import extract_links
        result = await extract_links(
            req.url,
            use_browser=req.use_browser,
            timeout=req.timeout,
        )
        if not result.get("links"):
            raise HTTPException(status_code=400, detail=f"No media links found for: {req.url}")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Extraction error: {str(e)}")


@app.post("/extract")
async def extract_post(request: Request):
    """Legacy yt-dlp extraction compatibility endpoint."""
    try:
        data = await request.json()
        url = data.get("url")
        if not url:
            return {"error": "Missing 'url' in JSON body", "formats": []}
            
        from plugins.helper.extractor import extract_raw_ytdlp
        result = await extract_raw_ytdlp(url)
        return result
    except Exception as e:
        return {"error": str(e), "formats": [], "title": "Extraction Failed"}

@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    """Health check — supports GET and HEAD (Koyeb probe)."""
    if app.is_shutting_down:
        raise HTTPException(status_code=503, detail="shutting_down")
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="starting")
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
