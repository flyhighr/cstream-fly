from fastapi import FastAPI, Response, BackgroundTasks, Request
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
import traceback

# Configure logging with more detailed format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)
logger = logging.getLogger("hls-proxy")

# Set higher log level for detailed debugging
logger.setLevel(logging.DEBUG)

app = FastAPI(title="HLS Stream Proxy", description="Reliable proxy for HLS streams with fallback mechanisms")

# Configuration
MAX_CACHE_SIZE = 200  
CACHE_EXPIRY = 120  
PLAYLIST_REFRESH_INTERVAL = 10  
HEALTH_CHECK_INTERVAL = 14 * 60  
FALLBACK_DELAY = 60  
MAX_RETRIES = 3  
RETRY_DELAY = 1  

# Cache storage
segment_cache: Dict[str, Dict[str, Any]] = {}
playlist_cache: Dict[str, Dict[str, Any]] = {}
fallback_segments: Dict[str, List[Dict[str, Any]]] = {}
health_status = {"status": "starting", "last_checked": datetime.now().isoformat()}
stream_errors = {"count": 0, "last_error": None}
request_log = []  # Store recent request logs

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
]

# Response models
class HealthResponse(BaseModel):
    status: str
    uptime: str
    cache_stats: Dict[str, int]
    errors: Dict[str, Any]
    last_checked: str
    recent_requests: List[Dict[str, Any]]

# Middleware to log all requests and responses
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    request_id = str(random.randint(10000, 99999))
    
    # Log request details
    logger.info(f"[{request_id}] Request: {request.method} {request.url.path} - Query params: {dict(request.query_params)}")
    
    try:
        response = await call_next(request)
        
        # Log response details
        process_time = time.time() - start_time
        logger.info(f"[{request_id}] Response: {response.status_code} - Took {process_time:.4f}s")
        
        # Store in request log
        log_entry = {
            "id": request_id,
            "method": request.method,
            "path": request.url.path,
            "query_params": dict(request.query_params),
            "status_code": response.status_code,
            "process_time": process_time,
            "timestamp": datetime.now().isoformat()
        }
        
        # Keep only the most recent 20 requests
        request_log.insert(0, log_entry)
        if len(request_log) > 20:
            request_log.pop()
            
        return response
    except Exception as e:
        logger.error(f"[{request_id}] Exception during request processing: {str(e)}")
        logger.error(traceback.format_exc())
        return Response(content=f"Internal server error: {str(e)}", status_code=500)

@app.on_event("startup")
async def startup_event():
    # Start the health check scheduler in a background thread
    def run_scheduler():
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error in scheduler: {str(e)}")
    
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
        logger.info("Performing self health check")
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Use the correct URL for self-health check
            response = await client.get("https://cstream-fly.onrender.com/health")
            logger.info(f"Self health check - Status: {response.status_code}")
            health_status["last_checked"] = datetime.now().isoformat()
            if response.status_code == 200:
                health_status["status"] = "healthy"
                logger.info("Self health check passed: Status healthy")
            else:
                health_status["status"] = "degraded"
                logger.warning(f"Self health check returned non-200 status: {response.status_code}")
    except Exception as e:
        logger.error(f"Self health check failed: {str(e)}")
        logger.error(traceback.format_exc())
        health_status["status"] = "error"
        health_status["last_error"] = str(e)

@app.get("/health")
async def get_health():
    """Health check endpoint"""
    logger.debug("Health check requested")
    start_time = os.environ.get("START_TIME", datetime.now().isoformat())
    try:
        uptime = str(datetime.now() - datetime.fromisoformat(start_time))
    except ValueError:
        # Handle potential ISO format issues
        uptime = "unknown"
        logger.warning(f"Could not parse start time: {start_time}")
    
    return HealthResponse(
        status=health_status["status"],
        uptime=uptime,
        cache_stats={
            "segments": len(segment_cache),
            "playlists": len(playlist_cache)
        },
        errors=stream_errors,
        last_checked=health_status["last_checked"],
        recent_requests=request_log[:10]  # Include recent requests in health response
    )

async def fetch_with_retry(url: str, is_binary: bool = False, attempts: int = MAX_RETRIES) -> Optional[httpx.Response]:
    """Fetch a URL with multiple retry attempts and fallback clients"""
    request_id = str(random.randint(10000, 99999))
    clients = [default_client, backup_client, long_timeout_client]
    errors = []
    
    logger.info(f"[{request_id}] Fetching URL: {url}, binary: {is_binary}")
    
    # Try each client
    for client_index, client in enumerate(clients):
        client_name = f"client_{client_index+1}"
        
        # Try multiple attempts
        for attempt in range(attempts):
            try:
                logger.debug(f"[{request_id}] Using {client_name}, attempt {attempt+1}/{attempts}")
                response = await client.get(url)
                
                logger.debug(f"[{request_id}] Response status: {response.status_code}, headers: {dict(response.headers)}")
                
                if response.status_code == 200:
                    logger.info(f"[{request_id}] Successfully fetched {url} with {client_name} on attempt {attempt+1}")
                    return response
                
                error_msg = f"Status code: {response.status_code}"
                logger.warning(f"[{request_id}] {error_msg}")
                errors.append(error_msg)
                
            except Exception as e:
                error_msg = f"Error with {client_name}: {str(e)}"
                logger.warning(f"[{request_id}] {error_msg}")
                errors.append(error_msg)
                
            # Wait before retry
            if attempt < attempts - 1:
                retry_time = RETRY_DELAY * (attempt + 1)
                logger.debug(f"[{request_id}] Waiting {retry_time}s before retry")
                await asyncio.sleep(retry_time)
    
    # Try alternate domains
    for domain_index, domain in enumerate(ALTERNATE_DOMAINS):
        try:
            original_domain = url.split("/")[2]
            if original_domain == domain:
                logger.debug(f"[{request_id}] Skipping alternate domain {domain} (same as original)")
                continue
                
            modified_url = url.replace(original_domain, domain)
            logger.info(f"[{request_id}] Trying alternate domain: {modified_url}")
            
            response = await default_client.get(modified_url)
            logger.debug(f"[{request_id}] Alternate domain response: {response.status_code}")
            
            if response.status_code == 200:
                logger.info(f"[{request_id}] Successfully fetched with alternate domain {domain}")
                return response
                
        except Exception as e:
            error_msg = f"Alternate domain {domain} error: {str(e)}"
            logger.warning(f"[{request_id}] {error_msg}")
            errors.append(error_msg)
    
    # Log the failure
    error_msg = f"All attempts to fetch {url} failed"
    logger.error(f"[{request_id}] {error_msg}")
    for i, error in enumerate(errors):
        logger.error(f"[{request_id}] Error {i+1}: {error}")
    
    stream_errors["count"] += 1
    stream_errors["last_error"] = f"{error_msg}: {', '.join(errors[:3])}"
    
    return None

def clean_old_cache_entries():
    """Remove old entries from the cache"""
    logger.debug("Cleaning old cache entries")
    current_time = time.time()
    
    # Clean segment cache
    expired_segments = [key for key, item in segment_cache.items() 
                       if current_time - item.get("timestamp", 0) > CACHE_EXPIRY]
    
    if expired_segments:
        logger.debug(f"Removing {len(expired_segments)} expired segments from cache")
        for key in expired_segments:
            del segment_cache[key]
    
    # If cache is still too large, remove oldest entries
    if len(segment_cache) > MAX_CACHE_SIZE:
        sorted_keys = sorted(segment_cache.keys(), 
                            key=lambda k: segment_cache[k].get("timestamp", 0))
        
        to_remove = sorted_keys[:len(segment_cache) - MAX_CACHE_SIZE]
        logger.debug(f"Cache too large, removing {len(to_remove)} oldest entries")
        
        for key in to_remove:
            del segment_cache[key]

def store_fallback_segment(quality: str, segment_data: Dict[str, Any]):
    """Store a segment as fallback content"""
    if quality not in fallback_segments:
        fallback_segments[quality] = []
        logger.debug(f"Created new fallback segment list for quality {quality}")
    
    # Keep up to 10 segments of fallback content
    fallback_segments[quality].append(segment_data)
    logger.debug(f"Added fallback segment for quality {quality}, now have {len(fallback_segments[quality])}")
    
    if len(fallback_segments[quality]) > 10:
        fallback_segments[quality].pop(0)
        logger.debug(f"Removed oldest fallback segment for quality {quality}")

@app.get("/proxy/playlist/{quality}")
async def proxy_playlist(quality: str, background_tasks: BackgroundTasks):
    """Proxy the m3u8 playlist file with fallback mechanisms"""
    request_id = str(random.randint(10000, 99999))
    logger.info(f"[{request_id}] Playlist requested for quality: {quality}")
    
    url = f"https://m3u8.cloudycx.net/media/hls/files/{quality}.m3u8"
    
    # Check if we need to refresh the cache
    current_time = time.time()
    refresh_needed = (
        quality not in playlist_cache or 
        current_time - playlist_cache[quality].get("timestamp", 0) > PLAYLIST_REFRESH_INTERVAL
    )
    
    if refresh_needed:
        logger.info(f"[{request_id}] Playlist cache refresh needed for {quality}")
        response = await fetch_with_retry(url)
        
        if response:
            content = response.text
            logger.debug(f"[{request_id}] Playlist content received: {content[:200]}...")
            
            # Store in cache
            playlist_cache[quality] = {
                "content": content,
                "timestamp": current_time
            }
            logger.info(f"[{request_id}] Refreshed playlist for {quality}")
            
            # Schedule cache cleanup
            background_tasks.add_task(clean_old_cache_entries)
        else:
            logger.error(f"[{request_id}] Failed to get playlist for {quality}")
            # If we have a cached version, use it even if expired
            if quality in playlist_cache:
                content = playlist_cache[quality]["content"]
                logger.info(f"[{request_id}] Using cached playlist for {quality} after fetch failure")
            else:
                logger.error(f"[{request_id}] No cached playlist available for {quality}")
                return Response(
                    content=f"Failed to get playlist and no cache available", 
                    status_code=503
                )
    else:
        content = playlist_cache[quality]["content"]
        logger.debug(f"[{request_id}] Using cached playlist for {quality} (valid for {int(PLAYLIST_REFRESH_INTERVAL - (current_time - playlist_cache[quality].get('timestamp', 0)))}s more)")
    
    # Return the playlist
    logger.info(f"[{request_id}] Returning playlist for {quality}")
    return Response(content=content, media_type="application/vnd.apple.mpegurl")

@app.get("/proxy/segment/{path:path}")
async def proxy_segment(path: str, background_tasks: BackgroundTasks):
    """Proxy the media segments with fallback mechanisms"""
    request_id = str(random.randint(10000, 99999))
    logger.info(f"[{request_id}] Segment requested: {path}")
    
    # Extract quality from path for fallback purposes
    quality = None
    for q in ["1080p", "720p", "480p", "360p"]:
        if q in path:
            quality = q
            break
    
    logger.debug(f"[{request_id}] Detected quality: {quality}")
    
    # Check if segment is in cache and not expired
    current_time = time.time()
    if path in segment_cache and current_time - segment_cache[path].get("timestamp", 0) <= CACHE_EXPIRY:
        logger.info(f"[{request_id}] Serving cached segment: {path}")
        return Response(
            content=segment_cache[path]["content"], 
            media_type=segment_cache[path]["content_type"]
        )
    
    # Construct the full URL
    url = f"https://ww3v.cloudycx.com/wordpress/hls/files/{path}"
    
    # Fetch the segment
    response = await fetch_with_retry(url, is_binary=True)
    
    if response:
        logger.info(f"[{request_id}] Successfully fetched segment: {path}")
        # Store in cache
        segment_data = {
            "content": response.content,
            "content_type": response.headers.get("content-type", "application/octet-stream"),
            "timestamp": current_time
        }
        
        segment_cache[path] = segment_data
        logger.debug(f"[{request_id}] Segment cached: {path}")
        
        # Store as fallback content if we can identify the quality
        if quality:
            background_tasks.add_task(store_fallback_segment, quality, segment_data)
        
        # Schedule cache cleanup
        background_tasks.add_task(clean_old_cache_entries)
        
        return Response(
            content=response.content,
            media_type=response.headers.get("content-type", "application/octet-stream")
        )
    else:
        logger.warning(f"[{request_id}] Failed to fetch segment: {path}, trying fallbacks")
        
        # Try to use fallback content
        if quality and quality in fallback_segments and fallback_segments[quality]:
            logger.info(f"[{request_id}] Using fallback segment for {quality} instead of {path}")
            # Use a random recent segment as fallback
            fallback = random.choice(fallback_segments[quality])
            return Response(
                content=fallback["content"],
                media_type=fallback["content_type"],
                headers={"X-Proxy-Fallback": "true"}
            )
        
        # If everything fails and we have an expired cache entry, use it
        if path in segment_cache:
            logger.info(f"[{request_id}] Using expired cached segment: {path}")
            return Response(
                content=segment_cache[path]["content"],
                media_type=segment_cache[path]["content_type"],
                headers={"X-Proxy-Expired": "true"}
            )
        
        # Complete failure
        logger.error(f"[{request_id}] Failed to get segment {path} and no fallbacks available")
        return Response(
            content=f"Failed to get segment and no fallbacks available",
            status_code=503
        )

@app.get("/")
async def root():
    """Root endpoint with basic server info"""
    return {
        "server": "HLS Stream Proxy",
        "status": health_status["status"],
        "version": "1.1.0",
        "endpoints": {
            "health": "/health",
            "playlist": "/proxy/playlist/{quality}",
            "segment": "/proxy/segment/{path}",
            "info": "/proxy/info",
            "logs": "/proxy/logs"
        }
    }

@app.get("/proxy/info")
async def proxy_info():
    """Return information about the proxy for debugging"""
    logger.debug("Proxy info requested")
    return {
        "cache_stats": {
            "segments": len(segment_cache),
            "playlists": len(playlist_cache),
            "fallback_segments": {k: len(v) for k, v in fallback_segments.items()}
        },
        "health": health_status,
        "errors": stream_errors,
        "config": {
            "MAX_CACHE_SIZE": MAX_CACHE_SIZE,
            "CACHE_EXPIRY": CACHE_EXPIRY,
            "PLAYLIST_REFRESH_INTERVAL": PLAYLIST_REFRESH_INTERVAL,
            "HEALTH_CHECK_INTERVAL": HEALTH_CHECK_INTERVAL,
            "MAX_RETRIES": MAX_RETRIES
        }
    }

@app.get("/proxy/logs")
async def get_logs():
    """Return recent request logs"""
    return {
        "logs": request_log
    }

@app.get("/proxy/clear-cache")
async def clear_cache():
    """Clear all caches - useful for debugging"""
    global segment_cache, playlist_cache, fallback_segments
    
    logger.info(f"Clearing caches: {len(segment_cache)} segments, {len(playlist_cache)} playlists, {len(fallback_segments)} fallback entries")
    
    segment_cache = {}
    playlist_cache = {}
    fallback_segments = {}
    
    return {
        "status": "success",
        "message": "All caches cleared"
    }

if __name__ == "__main__":
    import uvicorn
    os.environ["START_TIME"] = datetime.now().isoformat()
    port = int(os.environ.get("PORT", 8000))
    
    # Log startup information
    logger.info(f"Starting server on port {port}")
    logger.info(f"Debug mode: {logger.level == logging.DEBUG}")
    
    uvicorn.run(app, host="0.0.0.0", port=port)
