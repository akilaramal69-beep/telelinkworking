"""
extractor.py — Orchestrates media extraction using browser interception.
This module relies on Playwright for sniffing media URLs and httpx for validation.
"""

import asyncio
import re
import httpx
from typing import Optional
from .browser_extractor import intercept_browser, MEDIA_URL_PATTERNS


# Regex to identify media segments/chunks (usually low-value)
SEGMENT_PATTERNS = re.compile(
    r"[-_](seg|chunk|part|frag|fragment|track|init|video\d|audio\d)[-_]|\d+\.ts|\d+\.m4v",
    re.IGNORECASE,
)


async def extract_links(url: str, use_browser: bool = True, timeout: int = 25) -> dict:
    browser_results = []
    errors = []

    # ── Strategy 0: Check if URL is already a direct media link ────────────────
    is_direct = bool(MEDIA_URL_PATTERNS.search(url.split('?')[0]))

    # ── Strategy 1: Headless browser interception ──────────────────────────────
    if use_browser:
        try:
            if not is_direct:
                browser_results = await intercept_browser(url, timeout_ms=timeout * 1000)
            else:
                # Add the direct link to browser_results manually
                browser_results = [{
                    "url": url,
                    "stream_type": _guess_type_from_url(url),
                    "source": "direct_input"
                }]
        except Exception as e:
            errors.append(f"browser_error: {e}")

    if not browser_results:
        raise RuntimeError(
            f"Could not extract any links. Details — {'; '.join(errors)}"
        )

    # ── Build unified response ─────────────────────────────────────────────────
    response: dict = {
        "url": url,
        "title": "Extracted Video",
        "thumbnail": None,
        "duration": None,
        "extractor": "BrowserIntercept",
        "uploader": None,
    }

    # Merge browser links + yt-dlp formats
    # Filter browser results to remove likely ads (very small files that aren't HLS)
    filtered_browser_links = []
    AD_KEYWORDS = ("ads", "vast", "click", "pop", "preroll", "midroll", "postroll", "sponsored")
    
    for link in browser_results:
        # If it's a known ad domain or has ad keywords, skip it entirely
        if any(k in link["url"].lower() for k in AD_KEYWORDS):
            continue

        # 1DM Logic: Filter out likely segments/chunks unless it's the ONLY thing found
        is_segment = bool(SEGMENT_PATTERNS.search(link["url"]))
        if is_segment and link.get("stream_type") != "hls":
            continue

        # If it's HLS, we keep it (playlists are small)
        if link.get("stream_type") == "hls":
            filtered_browser_links.append(link)
            continue
            
        # If it has a known content length and it's tiny (< 1.5MB), it's likely an ad or segment
        length = link.get("content_length")
        if length and length < 1_500_000:
            continue
            
        # 1DM Hardening: Explicitly discard anything that looks like a webpage
        u_lower = link["url"].lower()
        if any(ext in u_lower for ext in (".html", ".htm", ".php", ".jsp", ".aspx")):
             # If it's a known media script, we still allow it
             if "remote_control.php" not in u_lower and "get_file" not in u_lower:
                 continue
                 
        filtered_browser_links.append(link)

    all_links = list(filtered_browser_links)

    # Deduplicate by URL
    seen = set()
    unique_links = []
    for link in all_links:
        u = link["url"]
        if u not in seen:
            seen.add(u)
            unique_links.append(link)

    # ── Final Layer: Asynchronous Validation (HEAD requests) ───────────────────
    # We only validate links that aren't already flagged as high-confidence media
    # OR any link that looks suspicious.
    async def _validate_link(link_item: dict) -> Optional[dict]:
        url = link_item["url"]
        
        # Skip validation for obviously media URLs (optimization)
        u_lower = url.lower().split('?')[0]
        if any(u_lower.endswith(ext) for ext in (".m3u8", ".mp4", ".mpd", ".webm")):
             return link_item
             
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
                headers = {"User-Agent": "Mozilla/5.0", "Referer": link_item.get("referer") or url}
                resp = await client.head(url, headers=headers)
                
                # If HEAD fails or is not allowed, try a range-request for 1 byte
                if resp.status_code >= 400:
                    headers["Range"] = "bytes=0-0"
                    resp = await client.get(url, headers=headers)
                
                ct = resp.headers.get("content-type", "").lower()
                
                # DISCARD HTML/JSON/TEXT
                if "text/html" in ct or "application/json" in ct or "text/plain" in ct:
                    return None
                    
                # If it's a media type, we keep it
                if "video" in ct or "audio" in ct or "mpegurl" in ct or "dash+xml" in ct:
                    link_item["content_type"] = ct
                    if not link_item.get("content_length"):
                        link_item["content_length"] = int(resp.headers.get("content-length", "0") or "0")
                    return link_item
                
                # If it's octet-stream we're cautious but usually keep it if it's not small
                if "octet-stream" in ct:
                     return link_item
                     
        except Exception:
            # If we can't validate, we're cautious: discard if it's a .php/.html/.aspx but keep otherwise
            if any(ext in u_lower for ext in (".php", ".html", ".aspx", ".jsp")):
                return None
            return link_item
            
        return None

    # Run validation in parallel
    validated_tasks = [asyncio.create_task(_validate_link(l)) for l in unique_links]
    validated_results = await asyncio.gather(*validated_tasks)
    final_links = [l for l in validated_results if l is not None]

    # Sort: combined streams first, then by height, then by filesize
    final_links.sort(
        key=lambda x: (
            bool(x.get("has_video") and x.get("has_audio")),
            x.get("height") or 0,
            x.get("filesize") or x.get("content_length") or 0,
        ),
        reverse=True,
    )

    response["links"] = final_links
    response["total"] = len(final_links)
    response["best_link"] = _pick_best(final_links)
    response["errors"] = errors if errors else None

    return response




def _pick_best(links: list) -> Optional[str]:
    if not links:
        return None
        
    # Strictly prefer HLS > video+audio > mp4
    # Expanded Ad-Shield: Check URL AND Referer for ad patterns
    AD_KEYWORDS = (
        "ads", "vast", "crossdomain", "traffic", "click", "pop", "pre-roll", 
        "mid-roll", "post-roll", "creative", "affiliate", "tracking", "pixel"
    )
    AD_DOMAINS = ("contentabc.com", "exoclick.com", "doubleclick.net", "googlesyndication.com")
    
    clean_links = []
    for l in links:
        url_lower = l["url"].lower()
        referer_lower = (l.get("referer") or "").lower()
        
        # If the URL or Referer matches an ad pattern, skip it
        if any(k in url_lower or k in referer_lower for k in AD_KEYWORDS):
            continue
        if any(d in url_lower or d in referer_lower for d in AD_DOMAINS):
            continue
            
        clean_links.append(l)
    
    target_links = clean_links if clean_links else links

    # 1. 1DM Preference: Direct Site Media (High Quality)
    # If we find a direct link on the same domain or a media script, favor it!
    for link in target_links:
        u = link["url"].lower()
        # Prioritize site-specific media scripts like remote_control.php or get_file
        if "remote_control.php" in u or "get_file" in u:
            return link["url"]

    # 2. Prefer JS Sniffer HLS
    for link in target_links:
        if link.get("source", "").startswith("js_") and ".m3u8" in link["url"]:
             return link["url"]

    # 3. Prefer Master Manifests
    MASTER_MANIFEST_KEYWORDS = ("master", "playlist", "index", "manifest", "m3u8", "main")
    for link in target_links:
        if link.get("stream_type") == "hls":
            u = link["url"].lower()
            if any(k in u for k in MASTER_MANIFEST_KEYWORDS):
                if not SEGMENT_PATTERNS.search(u):
                    return link["url"]
            
    # 4. Prefer regular HLS
    for link in target_links:
        if link.get("stream_type") == "hls":
            return link["url"]
            
    # 5. Prefer JS Sniffer MP4/WebM (usually the result of a menu click)
    for link in target_links:
        if link.get("source", "").startswith("js_") and link.get("stream_type") in ("mp4", "webm"):
             return link["url"]

    # 6. Prefer combined video+audio
    for link in target_links:
        if link.get("has_video") and link.get("has_audio"):
            return link["url"]
            
    # 7. Prefer MP4
    for link in target_links:
        if link.get("stream_type") == "mp4":
            return link["url"]
            
    # 8. Fallback to the first available link, but only if it's NOT just the input page
    # (Checking against common non-media patterns one last time)
    for link in target_links:
        u = link["url"].lower()
        if not any(k in u for k in (".php", ".html", ".htm", ".jsp", ".aspx")):
            return link["url"]
            
    return None # Return None if we can't find a high-confidence clean link


def _guess_type_from_url(url: str) -> str:
    path = url.split('?')[0].lower()
    if ".m3u8" in path: return "hls"
    if ".mpd" in path: return "dash"
    if ".mp4" in path: return "mp4"
    if ".webm" in path: return "webm"
    return "unknown"


async def extract_raw_ytdlp(url: str) -> dict:
    """
    Run browser interception and return results in the legacy yt-dlp format
    for drop-in compatibility with older bots.
    """
    try:
        # Call the main unified extraction logic
        res = await extract_links(url, use_browser=True, timeout=25)
        
        # Transform the unified response into the legacy format
        fake_info = {
            "id": "browser_extract",
            "title": res.get("title", "Extracted Video"),
            "thumbnail": res.get("thumbnail"),
            "duration": res.get("duration"),
            "extractor": "Playwright",
            "webpage_url": url,
            "formats": []
        }

        for i, link in enumerate(res.get("links", [])):
            fmt = {
                "format_id": f"browser_{i}",
                "url": link["url"],
                "ext": (link.get("content_type") or "video/mp4").split("/")[-1] or "mp4",
                "width": link.get("width", 1280),
                "height": link.get("height", 720),
                "vcodec": "avc1" if link.get("has_video") else "none",
                "acodec": "mp4a" if link.get("has_audio") else "none",
                "filesize": link.get("filesize") or link.get("content_length"),
                "source": link.get("source")
            }
            
            # Special handling for HLS
            if link.get("stream_type") == "hls":
                fmt["protocol"] = "m3u8_native"
                fmt["ext"] = "mp4"
                fmt["format_note"] = "HLS Stream"
                fmt["vcodec"] = "avc1"
                fmt["acodec"] = "mp4a"
            
            fake_info["formats"].append(fmt)

        return fake_info

    except Exception as e:
        raise ValueError(f"Unified extraction failed: {e}")
