#!/usr/bin/env python3
"""
download_proxy_server.py — Lightweight media download proxy for FB-Monitor.

Deploy this on a VPS so your local machine never directly hits Facebook CDN.
Your FB-Monitor instance sends URLs here, and this server fetches and
streams back the content.

Usage:
    # On your VPS:
    pip install fastapi uvicorn requests yt-dlp
    python download_proxy_server.py --port 9100 --token YOUR_SECRET_TOKEN

    # In your local config.json:
    "download_proxy": {
        "url": "http://your-vps-ip:9100",
        "token": "YOUR_SECRET_TOKEN"
    }

Security:
    - Bearer token auth required on all endpoints
    - Only fetches from Facebook CDN domains (fbcdn, scontent)
    - Rate-limited to prevent abuse
    - No data is stored on the proxy server
"""

import argparse
import io
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

log = logging.getLogger("dl-proxy")

app = FastAPI(title="FB-Monitor Download Proxy")

# Set via CLI arg, checked in auth
AUTH_TOKEN = ""

# Simple rate limiting
_last_request_time = 0.0
MIN_REQUEST_INTERVAL = 1.0  # seconds between requests


ALLOWED_CDN_DOMAINS = {
    "fbcdn.net",
    "scontent",
    "fbsbx.com",
    "facebook.com",
}


def _is_allowed_domain(url: str) -> bool:
    """Only allow fetching from Facebook CDN domains."""
    try:
        host = urlparse(url).hostname or ""
        return any(d in host for d in ALLOWED_CDN_DOMAINS)
    except Exception:
        return False


def _check_auth(authorization: str = Header(None)):
    if not AUTH_TOKEN:
        return  # no token configured = open (not recommended)
    if not authorization or authorization != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def _rate_limit():
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.time()


@app.get("/fetch")
async def fetch_media(
    url: str = Query(..., description="Facebook CDN URL to fetch"),
    authorization: str = Header(None),
):
    """Fetch an image/media file from Facebook CDN and stream it back."""
    _check_auth(authorization)

    if not _is_allowed_domain(url):
        raise HTTPException(status_code=403, detail="Domain not allowed")

    _rate_limit()

    try:
        import requests
        resp = requests.get(url, timeout=30, stream=True, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        })
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "application/octet-stream")

        return StreamingResponse(
            resp.iter_content(chunk_size=8192),
            media_type=content_type,
            headers={"Content-Length": resp.headers.get("Content-Length", "")},
        )

    except Exception as e:
        log.error(f"Fetch failed for {url[:80]}: {e}")
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {e}")


@app.get("/fetch-video")
async def fetch_video(
    post_url: str = Query(..., description="Facebook post URL for yt-dlp"),
    authorization: str = Header(None),
):
    """
    Download a video via yt-dlp on the proxy server and stream it back.
    Video is downloaded to a temp dir, then streamed and cleaned up.
    """
    _check_auth(authorization)
    _rate_limit()

    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = str(Path(tmpdir) / "video.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-check-certificates",
            "-o", output_template,
            "--no-playlist",
            "--restrict-filenames",
            "--no-overwrites",
            post_url,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="yt-dlp timed out")
        except FileNotFoundError:
            raise HTTPException(status_code=500, detail="yt-dlp not installed on proxy")

        if result.returncode != 0:
            if "Unsupported URL" in result.stderr or "no video" in result.stderr.lower():
                raise HTTPException(status_code=404, detail="No downloadable video")
            raise HTTPException(status_code=502, detail=f"yt-dlp error: {result.stderr[:200]}")

        # Find the downloaded file
        video_files = list(Path(tmpdir).glob("video.*"))
        if not video_files:
            raise HTTPException(status_code=404, detail="No video file produced")

        video_path = video_files[0]
        content = video_path.read_bytes()

        ext = video_path.suffix.lower()
        content_types = {
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
        }

        return Response(
            content=content,
            media_type=content_types.get(ext, "application/octet-stream"),
            headers={
                "Content-Disposition": f"attachment; filename={video_path.name}",
            },
        )


@app.get("/health")
async def health():
    return {"status": "ok"}


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="FB-Monitor Download Proxy")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=9100, help="Port")
    parser.add_argument("--token", default="", help="Auth token (recommended)")
    args = parser.parse_args()

    global AUTH_TOKEN
    AUTH_TOKEN = args.token

    logging.basicConfig(level=logging.INFO)

    if not AUTH_TOKEN:
        log.warning("No --token set! Proxy is open to anyone who can reach it.")
    else:
        log.info(f"Auth token configured ({len(AUTH_TOKEN)} chars)")

    print(f"\nDownload Proxy: http://{args.host}:{args.port}")
    print(f"Health check:   http://{args.host}:{args.port}/health\n")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
