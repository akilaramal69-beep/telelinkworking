import os
import asyncio
import time
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from plugins.config import Config
from utils.shared import WEBAPP_PROGRESS

# Initialize FastAPI
app = FastAPI(title="URL Uploader API")

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
        # FastAPI is async, so we can just await the coroutine directly if it's thread-safe
        # or run it on the bot's loop if it needs to interact with Pyrogram client safely.
        # Since fetch_ytdlp_formats is async and uses aiohttp, we just await it.
        # However, if it touches the bot client, we might need run_coroutine_threadsafe.
        # Let's use the bot_loop if available for consistency with the old app.py logic
        if app.bot_loop:
            future = asyncio.run_coroutine_threadsafe(fetch_ytdlp_formats(url), app.bot_loop)
            # We await the future in a thread-safe way
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, future.result, 60)
            return res
        else:
            res = await fetch_ytdlp_formats(url)
            return res
    except Exception as e:
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

@app.get("/grab")
async def grab_get(
    url: str,
    use_browser: bool = True,
    timeout: int = 25
):
    """Link-API compatibility endpoint for sniffing links."""
    try:
        from plugins.helper.extractor import extract_links
        result = await extract_links(url, use_browser=use_browser, timeout=timeout)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

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

@app.get("/health")
async def health():
    if app.is_shutting_down:
        raise HTTPException(status_code=503, detail="shutting_down")
    if not app.is_ready:
        raise HTTPException(status_code=503, detail="starting")
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
