from fastapi import FastAPI, Response, BackgroundTasks, Request
import httpx
import asyncio
import logging
import time
import os
import re
from datetime import datetime
from fastapi.middleware.cors import CORSMiddleware
import random
from typing import Dict, List, Optional, Any
from bs4 import BeautifulSoup

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
stream_url_cache = {"url": None, "timestamp": 0}

# HTTP clients
default_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)

# Known direct stream URLs that might work
DIRECT_STREAM_URLS = [
    "https://redx.embedxt.site/index.m3u8",  # From the iframe source
    "https://myww1.ruscfd.lat/hls/live2.m3u8" # Direct HLS stream
]

# Base segment URL
SEGMENT_BASE_URL = "https://myww1.ruscfd.lat"

async def extract_stream_url_from_iframe(html_content):
    """Extract the actual stream URL from iframe HTML content"""
    try:
        # Try to parse the HTML and find stream URL
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Look for video source elements
        for source in soup.find_all('source'):
            src = source.get('src')
            if src and (src.endswith('.m3u8') or src.endswith('.mpd')):
                logger.info(f"Found stream URL in source element: {src}")
                return src
        
        # If no source element found, look in script tags
        for script in soup.find_all('script'):
            script_text = script.string
            if script_text:
                # Look for common HLS or DASH URL patterns
                url_match = re.search(r'(https?://[^"\']+\.(m3u8|mpd))', script_text)
                if url_match:
                    logger.info(f"Found stream URL in script: {url_match.group(1)}")
                    return url_match.group(1)
        
        logger.warning("Could not find stream URL in iframe content")
        return None
    except Exception as e:
        logger.error(f"Error extracting stream URL from iframe: {str(e)}")
        return None

async def find_working_stream_url():
    """Try multiple methods to find a working stream URL"""
    # Check if we have a recent cached URL
    current_time = time.time()
    if stream_url_cache["url"] and current_time - stream_url_cache["timestamp"] < 300:  # 5 minutes
        logger.info(f"Using cached stream URL: {stream_url_cache['url']}")
        return stream_url_cache["url"]
    
    # First try the direct URLs we know about
    for url in DIRECT_STREAM_URLS:
        try:
            logger.info(f"Trying direct stream URL: {url}")
            response = await default_client.get(url)
            if response.status_code == 200 and response.text.startswith('#EXTM3U'):
                logger.info(f"Found working direct stream URL: {url}")
                stream_url_cache["url"] = url
                stream_url_cache["timestamp"] = current_time
                return url
        except Exception as e:
            logger.warning(f"Error checking direct URL {url}: {str(e)}")
    
    # If direct URLs don't work, try fetching the iframe
    try:
        iframe_url = "https://iframv3.embedxt.site/iframe/frame.php"
        logger.info(f"Fetching iframe URL: {iframe_url}")
        response = await default_client.get(iframe_url, headers={
            "Referer": "https://iframv3.embedxt.site/",
            "Origin": "https://iframv3.embedxt.site",
            "Host": "iframv3.embedxt.site"
        })
        
        if response.status_code == 200:
            # Try to extract the stream URL from the iframe content
            stream_url = await extract_stream_url_from_iframe(response.text)
            if stream_url:
                # Verify the stream URL works
                try:
                    stream_response = await default_client.get(stream_url)
                    if stream_response.status_code == 200 and stream_response.text.startswith('#EXTM3U'):
                        logger.info(f"Found working stream URL from iframe: {stream_url}")
                        stream_url_cache["url"] = stream_url
                        stream_url_cache["timestamp"] = current_time
                        return stream_url
                except Exception as e:
                    logger.warning(f"Error verifying stream URL from iframe: {str(e)}")
    except Exception as e:
        logger.error(f"Error fetching iframe: {str(e)}")
    
    # If all else fails, return the first direct URL as a fallback
    logger.warning("Could not find working stream URL, using fallback")
    return DIRECT_STREAM_URLS[0]

async def fetch_with_retry(url: str, is_binary: bool = False, attempts: int = MAX_RETRIES, headers=None):
    """Fetch a URL with multiple retry attempts"""
    request_id = str(random.randint(10000, 99999))
    errors = []
    
    logger.info(f"[{request_id}] Fetching URL: {url}")
    
    # Try with default client
    for attempt in range(attempts):
        try:
            logger.debug(f"[{request_id}] Attempt {attempt+1}/{attempts}")
            if headers:
                response = await default_client.get(url, headers=headers)
            else:
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

@app.get("/hls/live2.mpd")
@app.get("/hls/live2.m3u8")
@app.get("/media/hls/files/index.m3u8")
async def proxy_main_playlist():
    """Proxy the main m3u8 playlist file"""
    request_id = str(random.randint(10000, 99999))
    logger.info(f"[{request_id}] Main playlist requested")
    
    # Check if we need to refresh the cache
    current_time = time.time()
    refresh_needed = (
        "main" not in playlist_cache or 
        current_time - playlist_cache["main"].get("timestamp", 0) > PLAYLIST_REFRESH_INTERVAL
    )
    
    if refresh_needed:
        logger.info(f"[{request_id}] Main playlist cache refresh needed")
        
        # Find a working stream URL
        stream_url = await find_working_stream_url()
        
        if not stream_url:
            logger.error(f"[{request_id}] Failed to find working stream URL")
            return Response(
                content="Failed to find working stream URL",
                status_code=503,
                headers={"Access-Control-Allow-Origin": "*"}
            )
        
        # Add proper headers for the streaming site
        headers = {
            "Referer": "https://iframv3.embedxt.site/",
            "Origin": "https://iframv3.embedxt.site"
        }
        
        response = await fetch_with_retry(stream_url, headers=headers)
        
        if response:
            content = response.text
            
            # Verify this is actually an HLS playlist
            if not content.startswith('#EXTM3U'):
                logger.error(f"[{request_id}] Response is not a valid HLS playlist")
                if "main" in playlist_cache:
                    # Use cached playlist if available
                    modified_content = playlist_cache["main"]["content"]
                    logger.info(f"[{request_id}] Using cached playlist as fallback")
                else:
                    return Response(
                        content="Invalid playlist received from source",
                        status_code=503,
                        headers={"Access-Control-Allow-Origin": "*"}
                    )
            else:
                logger.debug(f"[{request_id}] Main playlist content received: {content[:200]}...")
                
                # Modify the playlist to point to our proxy for segments
                lines = content.split('\n')
                modified_lines = []
                
                for line in lines:
                    if line.startswith('#'):
                        # Keep all comment/directive lines as is
                        modified_lines.append(line)
                    elif line.startswith('http'):
                        # Replace direct segment URLs with our proxy
                        segment_url = line.strip()
                        segment_id = segment_url.split('/')[-1]
                        proxy_url = f"/proxy/segment/{segment_id}"
                        modified_lines.append(proxy_url)
                    elif line.strip() and not line.startswith('#'):
                        # This could be a relative segment URL
                        segment_id = line.strip()
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
                logger.info(f"[{request_id}] Refreshed main playlist")
        else:
            logger.error(f"[{request_id}] Failed to get main playlist")
            # If we have a cached version, use it even if expired
            if "main" in playlist_cache:
                modified_content = playlist_cache["main"]["content"]
                logger.info(f"[{request_id}] Using cached main playlist after fetch failure")
            else:
                logger.error(f"[{request_id}] No cached main playlist available")
                return Response(
                    content=f"Failed to get playlist and no cache available", 
                    status_code=503,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
    else:
        modified_content = playlist_cache["main"]["content"]
        logger.debug(f"[{request_id}] Using cached main playlist")
    
    # Return the modified playlist
    logger.info(f"[{request_id}] Returning main playlist")
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
    
    # Construct the segment URL using the known base URL
    url = f"{SEGMENT_BASE_URL}/{segment_id}"
    logger.info(f"[{request_id}] Fetching segment from: {url}")
    
    # Add proper headers for the streaming site
    headers = {
        "Referer": "https://iframv3.embedxt.site/",
        "Origin": "https://iframv3.embedxt.site"
    }
    
    # Fetch the segment
    response = await fetch_with_retry(url, is_binary=True, headers=headers)
    
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
                        
                        const playlistUrl = `${proxyBaseUrl}/hls/live2.m3u8`;
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
                        video.src = `${proxyBaseUrl}/hls/live2.m3u8`;
                        video.addEventListener('loadedmetadata', function() {
                            video.play().catch(err => console.error('Playback failed:', err));
                        });
                    } else {
                        loadingIndicator.textContent = 'Your browser does not support HLS playback';
                        console.error('HLS is not supported in this browser');
                    }
                }
                
                // Initialize stream
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
        "version": "2.0.0",
        "endpoints": {
            "embed": "/embed",
            "main_playlist": "/hls/live2.m3u8",
            "segment": "/proxy/segment/{segment_id}",
            "iframe_compatible": "/media/hls/files/index.m3u8"
        }
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
