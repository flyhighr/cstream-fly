from fastapi import FastAPI, Response, BackgroundTasks
import httpx
import asyncio
import logging
import time
import os
from datetime import datetime
from pydantic import BaseModel
import json
from typing import Dict, List, Optional, Any
import random
import schedule
import threading

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("hls-proxy")

app = FastAPI(title="HLS Stream Proxy", description="Reliable proxy for HLS streams with fallback mechanisms")

# Configuration
MAX_CACHE_SIZE = 200  # Maximum number of segments to cache
CACHE_EXPIRY = 120  # Seconds before a cached segment expires
PLAYLIST_REFRESH_INTERVAL = 10  # Seconds between playlist refreshes
HEALTH_CHECK_INTERVAL = 14 * 60  # 14 minutes for Render.com
FALLBACK_DELAY = 60  # Seconds of delay for fallback content
MAX_RETRIES = 3  # Number of retries for failed requests
RETRY_DELAY = 1  # Seconds between retries

# Cache storage
segment_cache: Dict[str, Dict[str, Any]] = {}
playlist_cache: Dict[str, Dict[str, Any]] = {}
fallback_segments: Dict[str, List[Dict[str, Any]]] = {}
health_status = {"status": "starting", "last_checked": datetime.now().isoformat()}
stream_errors = {"count": 0, "last_error": None}

# HTTP clients with different configurations
default_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
long_timeout_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
backup_client = httpx.AsyncClient(
    timeout=15.0, 
    follow_redirects=True,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
)

# List of alternate domains that might serve the same content
ALTERNATE_DOMAINS = [
    "ww3v.cloudycx.com",
    "m3u8.cloudycx.net"
    # Add more fallback domains if you discover any
]

# Response models
class HealthResponse(BaseModel):
    status: str
    uptime: str
    cache_stats: Dict[str, int]
    errors: Dict[str, Any]
    last_checked: str

@app.on_event("startup")
async def startup_event():
    # Start the health check scheduler in a background thread
    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(1)
    
    # Schedule the health check
    schedule.every(HEALTH_CHECK_INTERVAL).seconds.do(lambda: asyncio.run(health_check()))
    
    # Start the scheduler in a background thread
    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()
    
    # Log startup
    logger.info("Stream proxy service started")
    health_status["status"] = "running"

@app.on_event("shutdown")
async def shutdown_event():
    # Close HTTP clients
    await default_client.aclose()
    await long_timeout_client.aclose()
    await backup_client.aclose()
    logger.info("Stream proxy service stopped")

async def health_check():
    """Performs a self health check by requesting the health endpoint"""
    try:
        async with httpx.AsyncClient() as client:
            port = os.environ.get("PORT", 8000)
            response = await client.get(f"http://cstream-fly.onrender.com{port}/health")
            logger.info(f"Self health check - Status: {response.status_code}")
            health_status["last_checked"] = datetime.now().isoformat()
            if response.status_code == 200:
                health_status["status"] = "healthy"
            else:
                health_status["status"] = "degraded"
    except Exception as e:
        logger.error(f"Self health check failed: {str(e)}")
        health_status["status"] = "error"
        health_status["last_error"] = str(e)

@app.get("/health")
async def get_health():
    """Health check endpoint"""
    start_time = os.environ.get("START_TIME", datetime.now().isoformat())
    uptime = str(datetime.now() - datetime.fromisoformat(start_time))
    
    return HealthResponse(
        status=health_status["status"],
        uptime=uptime,
        cache_stats={
            "segments": len(segment_cache),
            "playlists": len(playlist_cache)
        },
        errors=stream_errors,
        last_checked=health_status["last_checked"]
    )

async def fetch_with_retry(url: str, is_binary: bool = False, attempts: int = MAX_RETRIES) -> Optional[httpx.Response]:
    """Fetch a URL with multiple retry attempts and fallback clients"""
    clients = [default_client, backup_client, long_timeout_client]
    errors = []
    
    # Try each client
    for client in clients:
        # Try multiple attempts
        for attempt in range(attempts):
            try:
                logger.debug(f"Fetching {url} with client {client.__class__.__name__}, attempt {attempt+1}")
                response = await client.get(url)
                if response.status_code == 200:
                    return response
                errors.append(f"Status code: {response.status_code}")
            except Exception as e:
                errors.append(f"Error: {str(e)}")
                logger.warning(f"Attempt {attempt+1} failed for {url}: {str(e)}")
                
            # Wait before retry
            if attempt < attempts - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))  # Exponential backoff
    
    # Try alternate domains
    for domain in ALTERNATE_DOMAINS:
        modified_url = url.replace(url.split("/")[2], domain)
        try:
            logger.debug(f"Trying alternate domain: {modified_url}")
            response = await default_client.get(modified_url)
            if response.status_code == 200:
                return response
        except Exception as e:
            errors.append(f"Alternate domain error: {str(e)}")
    
    # Log the failure
    error_msg = f"All attempts to fetch {url} failed: {', '.join(errors)}"
    logger.error(error_msg)
    stream_errors["count"] += 1
    stream_errors["last_error"] = error_msg
    
    return None

def clean_old_cache_entries():
    """Remove old entries from the cache"""
    current_time = time.time()
    
    # Clean segment cache
    expired_segments = [key for key, item in segment_cache.items() 
                       if current_time - item.get("timestamp", 0) > CACHE_EXPIRY]
    for key in expired_segments:
        del segment_cache[key]
    
    # If cache is still too large, remove oldest entries
    if len(segment_cache) > MAX_CACHE_SIZE:
        sorted_keys = sorted(segment_cache.keys(), 
                            key=lambda k: segment_cache[k].get("timestamp", 0))
        for key in sorted_keys[:len(segment_cache) - MAX_CACHE_SIZE]:
            del segment_cache[key]

def store_fallback_segment(quality: str, segment_data: Dict[str, Any]):
    """Store a segment as fallback content"""
    if quality not in fallback_segments:
        fallback_segments[quality] = []
    
    # Keep up to 30 seconds of fallback content
    fallback_segments[quality].append(segment_data)
    if len(fallback_segments[quality]) > 10:  # Assuming ~3s per segment
        fallback_segments[quality].pop(0)

@app.get("/proxy/playlist/{quality}")
async def proxy_playlist(quality: str, background_tasks: BackgroundTasks):
    """Proxy the m3u8 playlist file with fallback mechanisms"""
    url = f"https://m3u8.cloudycx.net/media/hls/files/{quality}.m3u8"
    
    # Check if we need to refresh the cache
    current_time = time.time()
    refresh_needed = (
        quality not in playlist_cache or 
        current_time - playlist_cache[quality].get("timestamp", 0) > PLAYLIST_REFRESH_INTERVAL
    )
    
    if refresh_needed:
        response = await fetch_with_retry(url)
        
        if response:
            content = response.text
            
            # Store in cache
            playlist_cache[quality] = {
                "content": content,
                "timestamp": current_time
            }
            logger.info(f"Refreshed playlist for {quality}")
            
            # Schedule cache cleanup
            background_tasks.add_task(clean_old_cache_entries)
        else:
            logger.error(f"Failed to get playlist for {quality}")
            # If we have a cached version, use it even if expired
            if quality in playlist_cache:
                content = playlist_cache[quality]["content"]
                logger.info(f"Using cached playlist for {quality} after fetch failure")
            else:
                return Response(
                    content=f"Failed to get playlist and no cache available", 
                    status_code=503
                )
    else:
        content = playlist_cache[quality]["content"]
        logger.debug(f"Using cached playlist for {quality}")
    
    # Return the playlist
    return Response(content=content, media_type="application/vnd.apple.mpegurl")

@app.get("/proxy/segment/{path:path}")
async def proxy_segment(path: str, background_tasks: BackgroundTasks):
    """Proxy the media segments with fallback mechanisms"""
    # Extract quality from path for fallback purposes
    quality = None
    for q in ["1080p", "720p", "480p", "360p"]:
        if q in path:
            quality = q
            break
    
    # Check if segment is in cache and not expired
    current_time = time.time()
    if path in segment_cache and current_time - segment_cache[path].get("timestamp", 0) <= CACHE_EXPIRY:
        logger.debug(f"Serving cached segment: {path}")
        return Response(
            content=segment_cache[path]["content"], 
            media_type=segment_cache[path]["content_type"]
        )
    
    url = f"https://ww3v.cloudycx.com/wordpress/hls/files/{path}"
    
    response = await fetch_with_retry(url, is_binary=True)
    
    if response:
        segment_data = {
            "content": response.content,
            "content_type": response.headers.get("content-type", "application/octet-stream"),
            "timestamp": current_time
        }
        
        segment_cache[path] = segment_data
        
        if quality:
            background_tasks.add_task(store_fallback_segment, quality, segment_data)
    
        background_tasks.add_task(clean_old_cache_entries)
        
        return Response(
            content=response.content,
            media_type=response.headers.get("content-type", "application/octet-stream")
        )
    else:
        if quality and quality in fallback_segments and fallback_segments[quality]:
            logger.warning(f"Using fallback segment for {quality} instead of {path}")
            fallback = random.choice(fallback_segments[quality])
            return Response(
                content=fallback["content"],
                media_type=fallback["content_type"],
                headers={"X-Proxy-Fallback": "true"}
            )
        
        if path in segment_cache:
            logger.warning(f"Using expired cached segment: {path}")
            return Response(
                content=segment_cache[path]["content"],
                media_type=segment_cache[path]["content_type"],
                headers={"X-Proxy-Expired": "true"}
            )
        
        logger.error(f"Failed to get segment {path} and no fallbacks available")
        return Response(
            content=f"Failed to get segment and no fallbacks available",
            status_code=503
        )

@app.get("/proxy/info")
async def proxy_info():
    """Return information about the proxy for debugging"""
    return {
        "cache_stats": {
            "segments": len(segment_cache),
            "playlists": len(playlist_cache),
            "fallback_segments": {k: len(v) for k, v in fallback_segments.items()}
        },
        "health": health_status,
        "errors": stream_errors
    }

if __name__ == "__main__":
    import uvicorn
    os.environ["START_TIME"] = datetime.now().isoformat()
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
