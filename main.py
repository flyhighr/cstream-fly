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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)
logger = logging.getLogger("hls-proxy")
logger.setLevel(logging.DEBUG)

app = FastAPI(title="HLS Stream Proxy", description="Reliable proxy for HLS streams")

app.add_middleware(
    CORSMiddleware,
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)
logger = logging.getLogger("hls-proxy")
logger.setLevel(logging.DEBUG)

app = FastAPI(title="HLS Stream Proxy", description="Reliable proxy for HLS streams")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"],  
)

CACHE_EXPIRY = 120  
PLAYLIST_REFRESH_INTERVAL = 5  
MAX_RETRIES = 3

segment_cache: Dict[str, Dict[str, Any]] = {}
playlist_cache: Dict[str, Dict[str, Any]] = {}
m3u8_cache: Dict[str, Dict[str, Any]] = {}

default_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)

KNOWN_DOMAINS = [
    "m3u8x2.cloudycx.net",
    "ww3v.cloudycx.com",
    "cloudycx.com",
    "cloudycx.net",
    "embedxt.site"
]

async def fetch_with_retry(url: str, is_binary: bool = False, attempts: int = MAX_RETRIES, headers=None):
    """Fetch a URL with multiple retry attempts"""
    request_id = str(random.randint(10000, 99999))
    errors = []

    logger.info(f"[{request_id}] Fetching URL: {url}")

    if not headers:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Referer": "https://embedxt.site/",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://embedxt.site"
        }

    for attempt in range(attempts):
        try:
            logger.debug(f"[{request_id}] Attempt {attempt+1}/{attempts}")
            response = await default_client.get(url, headers=headers)

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

        if attempt < attempts - 1:
            await asyncio.sleep(1)

    if any(domain in url for domain in KNOWN_DOMAINS):

        parts = url.split("/")
        filename = parts[-1]
        path = "/".join(parts[3:-1]) if len(parts) > 4 else ""

        for domain in KNOWN_DOMAINS:
            if domain in url:
                continue  

            try:
                alt_url = f"https://{domain}/{path}/{filename}" if path else f"https://{domain}/{filename}"
                logger.info(f"[{request_id}] Trying alternate domain: {alt_url}")

                response = await default_client.get(alt_url, headers=headers)
                if response.status_code == 200:
                    logger.info(f"[{request_id}] Successfully fetched with alternate domain {domain}")
                    return response

            except Exception as e:
                logger.warning(f"[{request_id}] Alternate domain {domain} error: {str(e)}")

    logger.error(f"[{request_id}] All attempts to fetch {url} failed")
    return None

@app.get("/proxy/m3u8/{quality}")
async def proxy_m3u8(quality: str, background_tasks: BackgroundTasks):
    """Proxy the m3u8 playlist file"""
    request_id = str(random.randint(10000, 99999))
    logger.info(f"[{request_id}] M3U8 requested for quality: {quality}")

    url = f"https://m3u8x2.cloudycx.net/media/hls/files/{quality}.m3u8"

    current_time = time.time()
    refresh_needed = (
        quality not in m3u8_cache or 
        current_time - m3u8_cache[quality].get("timestamp", 0) > PLAYLIST_REFRESH_INTERVAL
    )

    if refresh_needed:
        logger.info(f"[{request_id}] M3U8 cache refresh needed for {quality}")
        response = await fetch_with_retry(url)

        if response:
            content = response.text
            logger.debug(f"[{request_id}] M3U8 content received: {content[:200]}...")

            lines = content.split('\n')
            modified_lines = []

            for line in lines:
                if line.startswith('#'):

                    modified_lines.append(line)
                elif line.startswith('http'):

                    segment_id = line.split('/')[-1]
                    proxy_url = f"/proxy/segment/{segment_id}"
                    modified_lines.append(proxy_url)
                elif line.strip():  
                    modified_lines.append(line)

            modified_content = '\n'.join(modified_lines)

            m3u8_cache[quality] = {
                "content": modified_content,
                "timestamp": current_time
            }
            logger.info(f"[{request_id}] Refreshed M3U8 for {quality}")
        else:
            logger.error(f"[{request_id}] Failed to get M3U8 for {quality}")

            if quality in m3u8_cache:
                modified_content = m3u8_cache[quality]["content"]
                logger.info(f"[{request_id}] Using cached M3U8 for {quality} after fetch failure")
            else:
                logger.error(f"[{request_id}] No cached M3U8 available for {quality}")
                return Response(
                    content=f"Failed to get M3U8 and no cache available", 
                    status_code=503,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
    else:
        modified_content = m3u8_cache[quality]["content"]
        logger.debug(f"[{request_id}] Using cached M3U8 for {quality}")

    logger.info(f"[{request_id}] Returning M3U8 for {quality}")
    return Response(
        content=modified_content, 
        media_type="application/vnd.apple.mpegurl",
        headers={"Access-Control-Allow-Origin": "*"}
    )

@app.get("/proxy/segment/{segment_id}")
async def proxy_segment(segment_id: str):
    """Proxy the media segments (HTML/JS files)"""
    request_id = str(random.randint(10000, 99999))
    logger.info(f"[{request_id}] Segment requested: {segment_id}")

    current_time = time.time()
    if segment_id in segment_cache and current_time - segment_cache[segment_id].get("timestamp", 0) <= CACHE_EXPIRY:
        logger.info(f"[{request_id}] Serving cached segment: {segment_id}")
        return Response(
            content=segment_cache[segment_id]["content"], 
            media_type=segment_cache[segment_id]["content_type"],
            headers={"Access-Control-Allow-Origin": "*"}
        )

    url = f"https://ww3v.cloudycx.com/wordpress/hls/files/{segment_id}"

    response = await fetch_with_retry(url, is_binary=True)

    if response:
        logger.info(f"[{request_id}] Successfully fetched segment: {segment_id}")

        content_type = "application/octet-stream"
        if segment_id.endswith(".js"):
            content_type = "application/javascript"
        elif segment_id.endswith(".html"):
            content_type = "text/html"
        elif segment_id.endswith(".ts"):
            content_type = "video/mp2t"

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

@app.get("/index.m3u8")
async def main_m3u8():
    """Serve the main m3u8 file that points to our proxy"""
    content = """#EXTM3U

/proxy/m3u8/1080p

/proxy/m3u8/720p

/proxy/m3u8/480p

/proxy/m3u8/360p
"""
    return Response(
        content=content,
        media_type="application/vnd.apple.mpegurl",
        headers={"Access-Control-Allow-Origin": "*"}
    )

@app.get("/iframe")
async def iframe_player():
    """Serve an iframe player that uses our proxy"""
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, maximum-scale=1.0, user-scalable=no">
    <meta http-equiv="X-UA-Compatible" content="IE=edge,chrome=1">
    <link rel="stylesheet" href="https://cdn.plyr.io/3.6.2/plyr.css">
    <link href="https://cdn.jsdelivr.net/gh/halfmoonui/halfmoon@1.0.4/css/halfmoon.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script src="https://cdn.plyr.io/3.6.2/plyr.polyfilled.js"></script>
    <style>
        body, html {
            margin: 0;
            padding: 0;
            width: 100%;
            height: 100%;
            overflow: hidden;
            background-color: 
        }
        video {
            width: 100%;
            height: 100%;
        }
    </style>
    <script>
        document.addEventListener("DOMContentLoaded", async () => {
            const videoElement = document.querySelector("video");
            const videoSource = videoElement.getElementsByTagName("source")[0].src;

            try {
                // Fetch the resource in no-cors mode
                await fetch(videoSource, { mode: 'no-cors' });
                // Load the video URL directly into the player
                if (Hls.isSupported()) {
                    const hlsConfig = { 
                        maxMaxBufferLength: 100,
                        maxBufferSize: 30 * 1000 * 1000,
                        maxBufferLength: 30,
                        enableWorker: true
                    };
                    const hls = new Hls(hlsConfig);
                    hls.loadSource(videoSource);

                    hls.on(Hls.Events.MANIFEST_PARSED, (event, data) => {
                        const qualities = hls.levels.map(level => level.height);

                        const plyrOptions = {
                            quality: {
                                default: qualities[0],
                                options: qualities,
                                forced: true,
                                onChange: newQuality => {
                                    hls.levels.forEach((level, index) => {
                                        if (level.height === newQuality) {
                                            hls.currentLevel = index;
                                        }
                                    });
                                }
                            }
                        };

                        new Plyr(videoElement, plyrOptions);
                    });

                    hls.attachMedia(videoElement);
                    window.hls = hls;
                } else {
                    new Plyr(videoElement, {});
                }
            } catch (error) {
                console.error("Error loading the video:", error);
            }
        });
    </script>
</head>
<body class="dark-mode with-custom-scrollbars with-custom-css-scrollbars">
    <video id="player" controls preload="metadata" poster="https://www.icecric.news/wp-content/uploads/2024/12/unnamed-3.webp" class="plyr">
        <source src="/index.m3u8" type="application/x-mpegURL">
    </video>
</body>
</html>
    """

    return Response(
        content=html_content,
        media_type="text/html",
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
    <link rel="stylesheet" href="https://cdn.plyr.io/3.6.2/plyr.css">
    <style>
        body, html {
            margin: 0;
            padding: 0;
            width: 100%;
            height: 100%;
            overflow: hidden;
            background-color: 
        }

            width: 100%;
            height: 100%;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            color: white;
        }
        video {
            width: 100%;
            height: 100%;
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
    <script src="https://cdn.plyr.io/3.6.2/plyr.polyfilled.js"></script>
    <script>
        document.addEventListener('DOMContentLoaded', function() {
            const video = document.getElementById('video');
            const loadingIndicator = document.getElementById('loading');
            const proxyBaseUrl = window.location.origin;
            const playlistUrl = `${proxyBaseUrl}/index.m3u8`;

            // Function to load the stream
            function loadStream() {
                // Show loading indicator
                loadingIndicator.style.display = 'block';

                if (Hls.isSupported()) {
                    const hls = new Hls({
                        debug: false,
                        enableWorker: true,
                        maxBufferSize: 30 * 1000 * 1000,
                        maxBufferLength: 30
                    });

                    console.log("Loading playlist:", playlistUrl);

                    hls.loadSource(playlistUrl);
                    hls.attachMedia(video);

                    hls.on(Hls.Events.MANIFEST_PARSED, function(event, data) {
                        console.log('Manifest parsed, trying to play');
                        loadingIndicator.style.display = 'none';

                        // Setup Plyr
                        const qualities = hls.levels.map(level => level.height);
                        const plyrOptions = {
                            quality: {
                                default: qualities[0],
                                options: qualities,
                                forced: true,
                                onChange: newQuality => {
                                    hls.levels.forEach((level, index) => {
                                        if (level.height === newQuality) {
                                            hls.currentLevel = index;
                                        }
                                    });
                                }
                            }
                        };

                        const player = new Plyr(video, plyrOptions);

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

                    window.hls = hls;
                } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                    // For Safari
                    loadingIndicator.style.display = 'none';
                    video.src = playlistUrl;
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

            // Try to unlock screen orientation for mobile
            if (screen.orientation && screen.orientation.unlock) {
                screen.orientation.unlock().catch(err => {
                    console.error('Failed to unlock screen orientation:', err);
                });
            }
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
            "iframe": "/iframe",
            "main_m3u8": "/index.m3u8",
            "quality_m3u8": "/proxy/m3u8/{quality}",
            "segment": "/proxy/segment/{segment_id}"
        }
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
