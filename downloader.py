"""
downloader.py — Download images and videos from Facebook posts.

Uses yt-dlp for videos (handles Facebook's video player)
and requests for direct image downloads.
"""

import logging
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("fb-monitor")

try:
    import requests
except ImportError:
    requests = None
    log.warning("requests not installed — image downloads disabled")


def download_images(image_urls: list[str], output_dir: Path) -> list[str]:
    """
    Download images from Facebook CDN URLs.
    Returns list of saved file paths.
    """
    if not requests:
        log.warning("Skipping image downloads (requests not installed)")
        return []

    saved = []
    output_dir.mkdir(parents=True, exist_ok=True)

    for i, url in enumerate(image_urls):
        try:
            # Determine extension from URL
            parsed = urlparse(url)
            path = parsed.path
            ext = Path(path).suffix or ".jpg"
            if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                ext = ".jpg"

            filename = f"image_{i + 1}{ext}"
            filepath = output_dir / filename

            # Skip if already downloaded
            if filepath.exists():
                saved.append(str(filepath))
                continue

            log.info(f"  Downloading image {i + 1}/{len(image_urls)}")
            resp = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            })
            resp.raise_for_status()

            filepath.write_bytes(resp.content)
            saved.append(str(filepath))
            log.info(f"  Saved: {filepath}")

        except Exception as e:
            log.warning(f"  Failed to download image {url[:80]}: {e}")

    return saved


def download_video_ytdlp(post_url: str, output_dir: Path) -> list[str]:
    """
    Download video from a Facebook post using yt-dlp.
    Returns list of saved file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / "video_%(autonumber)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-check-certificates",
        "-o", output_template,
        "--no-playlist",
        "--restrict-filenames",
        "--no-overwrites",
        post_url,
    ]

    log.info(f"  Downloading video via yt-dlp...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

        if result.returncode == 0:
            # Find downloaded files from yt-dlp output
            saved = []
            for line in result.stdout.splitlines():
                if "Destination:" in line:
                    path = line.split("Destination:")[-1].strip()
                    saved.append(path)
                elif "has already been downloaded" in line:
                    path = line.split("[download]")[-1].split("has already")[0].strip()
                    saved.append(path)

            if not saved:
                # Check output dir for any video files
                for ext in ("*.mp4", "*.mkv", "*.webm"):
                    saved.extend(str(p) for p in output_dir.glob(ext))

            log.info(f"  Video download complete: {len(saved)} file(s)")
            return saved
        else:
            # yt-dlp might fail for non-video posts — that's fine
            if "Unsupported URL" in result.stderr or "no video" in result.stderr.lower():
                log.debug(f"  No downloadable video at this URL")
            else:
                log.warning(f"  yt-dlp error: {result.stderr[:200]}")
            return []

    except subprocess.TimeoutExpired:
        log.warning("  yt-dlp timed out after 180s")
        return []
    except FileNotFoundError:
        log.error("  yt-dlp not found. Install: pip install yt-dlp")
        return []


def download_attachments(
    post_url: str,
    image_urls: list[str],
    video_urls: list[str],
    output_dir: Path,
) -> dict:
    """
    Download all attachments for a post.

    Returns:
        {
            "images": [list of saved image paths],
            "videos": [list of saved video paths],
        }
    """
    attachments_dir = output_dir / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    result = {"images": [], "videos": []}

    # Download images directly
    if image_urls:
        result["images"] = download_images(image_urls, attachments_dir)

    # Download videos — prefer yt-dlp on the post URL since it handles
    # Facebook's video player better than direct URLs
    is_video_post = any(p in post_url for p in ("/videos/", "/watch/", "/reel/"))
    if is_video_post or video_urls:
        result["videos"] = download_video_ytdlp(post_url, attachments_dir)

    total = len(result["images"]) + len(result["videos"])
    if total > 0:
        log.info(f"  📎 Downloaded {len(result['images'])} images, {len(result['videos'])} videos")

    return result
