from fastapi import FastAPI, Response, BackgroundTasks, Request
import httpx
import asyncio
import logging
import time
import os
from datetime import datetime
from fastapi.middleware.cors import CORSMiddleware
import random
from typing import Dict, List, Optional, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)
logger = logging.getLogger("hls-proxy")
logger.setLevel(logging.DEBUG)

app = FastAPI(title="HLS Stream Proxy", description="Reliable proxy for HLS streams")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)

# Configuration
CACHE_EXPIRY = 120  # 2 minutes
PLAYLIST_REFRESH_INTERVAL = 5  # 5 seconds for playlist
MAX_RETRIES = 3

# Cache storage
segment_cache: Dict[str, Dict[str, Any]] = {}
playlist_cache: Dict[str, Dict[str, Any]] = {}

# HTTP clients
default_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)

# Primary stream sources
PRIMARY_PLAYLIST_URL = "https://xhls.embedxt.site/hls/live2.mp"
SEGMENT_BASE_URL = "https://myww1.ruscfd.lat"

async def fetch_with_retry(url: str, is_binary: bool = False, attempts: int = MAX_RETRIES):
    """Fetch a URL with multiple retry attempts"""
    request_id = str(random.randint(10000, 99999))
    errors = []
    
    logger.info(f"[{request_id}] Fetching URL: {url}")
    
    # Try with default client
    for attempt in range(attempts):
        try:
            logger.debug(f"[{request_id}] Attempt {attempt+1}/{attempts}")
            response = await default_client.get(url)
            
            if response.status_code == 200:
                logger.info(f"[{request_id}] Successfully fetched {url}")
                return response
            
            error_msg = f"Status code: {response.status_code}"
            logger.warning(f"[{request_id}] {error_msg}")
            errors.append(error_msg)
            
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.warning(f"[{request_id}] {error_msg}")
            errors.append(error_msg)
            
        # Wait before retry
        if attempt < attempts - 1:
            await asyncio.sleep(1)
    
    logger.error(f"[{request_id}] All attempts to fetch {url} failed")
    return None

@app.get("/proxy/playlist")
async def proxy_playlist(background_tasks: BackgroundTasks):
    """Proxy the m3u8 playlist file"""
    request_id = str(random.randint(10000, 99999))
    logger.info(f"[{request_id}] Playlist requested")
    
    # Check if we need to refresh the cache
    current_time = time.time()
    refresh_needed = (
        "main" not in playlist_cache or 
        current_time - playlist_cache["main"].get("timestamp", 0) > PLAYLIST_REFRESH_INTERVAL
    )
    
    if refresh_needed:
        logger.info(f"[{request_id}] Playlist cache refresh needed")
        response = await fetch_with_retry(PRIMARY_PLAYLIST_URL)
        
        if response:
            content = response.text
            logger.debug(f"[{request_id}] Playlist content received: {content[:200]}...")
            
            # Modify the playlist to point to our proxy for segments
            lines = content.split('\n')
            modified_lines = []
            
            for line in lines:
                if line.startswith('#'):
                    # Keep all comment/directive lines as is
                    modified_lines.append(line)
                elif line.startswith('http'):
                    # Replace direct segment URLs with our proxy
                    segment_id = line.split('/')[-1]
                    proxy_url = f"/proxy/segment/{segment_id}"
                    modified_lines.append(proxy_url)
                else:
                    modified_lines.append(line)
            
            modified_content = '\n'.join(modified_lines)
            
            # Store in cache
            playlist_cache["main"] = {
                "content": modified_content,
                "timestamp": current_time
            }
            logger.info(f"[{request_id}] Refreshed playlist")
        else:
            logger.error(f"[{request_id}] Failed to get playlist")
            # If we have a cached version, use it even if expired
            if "main" in playlist_cache:
                modified_content = playlist_cache["main"]["content"]
                logger.info(f"[{request_id}] Using cached playlist after fetch failure")
            else:
                logger.error(f"[{request_id}] No cached playlist available")
                return Response(
                    content=f"Failed to get playlist and no cache available", 
                    status_code=503,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
    else:
        modified_content = playlist_cache["main"]["content"]
        logger.debug(f"[{request_id}] Using cached playlist")
    
    # Return the modified playlist
    logger.info(f"[{request_id}] Returning playlist")
    return Response(
        content=modified_content, 
        media_type="application/vnd.apple.mpegurl",
        headers={"Access-Control-Allow-Origin": "*"}
    )

@app.get("/proxy/segment/{segment_id}")
async def proxy_segment(segment_id: str):
    """Proxy the media segments"""
    request_id = str(random.randint(10000, 99999))
    logger.info(f"[{request_id}] Segment requested: {segment_id}")
    
    # Check if segment is in cache and not expired
    current_time = time.time()
    if segment_id in segment_cache and current_time - segment_cache[segment_id].get("timestamp", 0) <= CACHE_EXPIRY:
        logger.info(f"[{request_id}] Serving cached segment: {segment_id}")
        return Response(
            content=segment_cache[segment_id]["content"], 
            media_type=segment_cache[segment_id]["content_type"],
            headers={"Access-Control-Allow-Origin": "*"}
        )
    
    # Construct the URL for the segment
    url = f"{SEGMENT_BASE_URL}/{segment_id}"
    
    # Fetch the segment
    response = await fetch_with_retry(url, is_binary=True)
    
    if response:
        logger.info(f"[{request_id}] Successfully fetched segment: {segment_id}")
        
        # Determine content type based on response
        content_type = response.headers.get("content-type", "application/octet-stream")
        
        # Store in cache
        segment_cache[segment_id] = {
            "content": response.content,
            "content_type": content_type,
            "timestamp": current_time
        }
        
        return Response(
            content=response.content,
            media_type=content_type,
            headers={"Access-Control-Allow-Origin": "*"}
        )
    else:
        logger.error(f"[{request_id}] Failed to get segment {segment_id}")
        return Response(
            content=f"Failed to get segment",
            status_code=503,
            headers={"Access-Control-Allow-Origin": "*"}
        )

@app.get("/embed")
async def embed_player():
    """Serve an embedded player that uses our proxy"""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Stream Player</title>
        <style>
            body, html {
                margin: 0;
                padding: 0;
                width: 100%;
                height: 100%;
                overflow: hidden;
                background-color: #000;
            }
            #player-container {
                width: 100%;
                height: 100%;
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                color: white;
            }
            video {
                max-width: 100%;
                max-height: 100%;
            }
            .loading {
                position: absolute;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                font-size: 16px;
                color: white;
            }
        </style>
    </head>
    <body>
        <div id="player-container">
            <video id="video" controls autoplay playsinline></video>
            <div class="loading" id="loading">Loading stream...</div>
        </div>
        
        <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
        <script>
            document.addEventListener('DOMContentLoaded', function() {
                const video = document.getElementById('video');
                const loadingIndicator = document.getElementById('loading');
                const proxyBaseUrl = window.location.origin;
                
                let hls = null;
                
                // Function to load the stream
                function loadStream() {
                    // Show loading indicator
                    loadingIndicator.style.display = 'block';
                    
                    // Destroy previous Hls instance if it exists
                    if (hls) {
                        hls.destroy();
                    }
                    
                    if (Hls.isSupported()) {
                        hls = new Hls({
                            debug: false,
                            enableWorker: true,
                            lowLatencyMode: true,
                            backBufferLength: 90
                        });
                        
                        const playlistUrl = `${proxyBaseUrl}/proxy/playlist`;
                        console.log("Loading playlist:", playlistUrl);
                        
                        hls.loadSource(playlistUrl);
                        hls.attachMedia(video);
                        
                        hls.on(Hls.Events.MANIFEST_PARSED, function() {
                            console.log('Manifest parsed, trying to play');
                            loadingIndicator.style.display = 'none';
                            video.play().catch(err => {
                                console.error('Playback failed:', err);
                                alert('Playback failed. Please try again.');
                            });
                        });
                        
                        // Handle errors
                        hls.on(Hls.Events.ERROR, function(event, data) {
                            console.error('HLS error:', data);
                            if (data.fatal) {
                                switch(data.type) {
                                    case Hls.ErrorTypes.NETWORK_ERROR:
                                        console.log('Network error, trying to recover...');
                                        hls.startLoad();
                                        break;
                                    case Hls.ErrorTypes.MEDIA_ERROR:
                                        console.log('Media error, trying to recover...');
                                        hls.recoverMediaError();
                                        break;
                                    default:
                                        console.error('Fatal error, cannot recover');
                                        loadingIndicator.textContent = 'Stream error. Trying again in 5 seconds...';
                                        setTimeout(() => {
                                            loadStream();
                                        }, 5000);
                                        break;
                                }
                            }
                        });
                    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                        // For Safari
                        loadingIndicator.style.display = 'none';
                        video.src = `${proxyBaseUrl}/proxy/playlist`;
                        video.addEventListener('loadedmetadata', function() {
                            video.play().catch(err => console.error('Playback failed:', err));
                        });
                    } else {
                        loadingIndicator.textContent = 'Your browser does not support HLS playback';
                        console.error('HLS is not supported in this browser');
                    }
                }
                
                // Initialize with default quality
                loadStream();
                
                // Auto-reload stream if it stalls
                video.addEventListener('stalled', function() {
                    console.log('Stream stalled, reloading...');
                    loadingIndicator.textContent = 'Stream stalled. Reloading...';
                    loadingIndicator.style.display = 'block';
                    setTimeout(() => {
                        loadStream();
                    }, 2000);
                });
                
                // Refresh stream periodically to avoid stalling
                setInterval(() => {
                    if (hls && video.paused) {
                        console.log('Refreshing stalled stream...');
                        hls.startLoad();
                    }
                }, 30000);
            });
        </script>
    </body>
    </html>
    """
    
    return Response(
        content=html_content,
        media_type="text/html",
        headers={"Access-Control-Allow-Origin": "*"}
    )

@app.get("/")
async def root():
    """Root endpoint with basic server info"""
    return {
        "server": "HLS Stream Proxy",
        "version": "1.3.0",
        "endpoints": {
            "embed": "/embed",
            "playlist": "/proxy/playlist",
            "segment": "/proxy/segment/{segment_id}"
        }
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
