"""
downloader.py — Download images and videos from Facebook posts.

Uses yt-dlp for videos (handles Facebook's video player)
and requests for direct image downloads.

Supports three download modes:
  1. Direct — fetch from Facebook CDN (default)
  2. SOCKS5 proxy — route through Tor
  3. Remote proxy — fetch via a VPS running download_proxy_server.py

Adds random delays between downloads to avoid burst patterns.
"""

import logging
import random
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse, quote

log = logging.getLogger("fb-monitor")

try:
    import requests
except ImportError:
    requests = None
    log.warning("requests not installed — image downloads disabled")


def _get_proxy_dict(proxy_url: str = "") -> dict:
    """Build a requests-compatible proxies dict from a SOCKS5 URL."""
    if not proxy_url:
        return {}
    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def _remote_proxy_headers(token: str) -> dict:
    """Build auth headers for the remote download proxy."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _download_image_via_proxy(
    cdn_url: str,
    filepath: Path,
    proxy_config: dict,
) -> bool:
    """
    Download an image via the remote download proxy server.
    Returns True on success.
    """
    if not requests:
        return False

    base_url = proxy_config["url"].rstrip("/")
    token = proxy_config.get("token", "")

    try:
        resp = requests.get(
            f"{base_url}/fetch",
            params={"url": cdn_url},
            headers=_remote_proxy_headers(token),
            timeout=60,
            stream=True,
        )
        resp.raise_for_status()

        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True

    except Exception as e:
        log.warning(f"  Remote proxy image fetch failed: {e}")
        return False


def _download_video_via_proxy(
    post_url: str,
    output_dir: Path,
    proxy_config: dict,
) -> list[str]:
    """
    Download a video via the remote download proxy server.
    The proxy runs yt-dlp on the VPS and streams the file back.
    Returns list of saved file paths.
    """
    if not requests:
        return []

    base_url = proxy_config["url"].rstrip("/")
    token = proxy_config.get("token", "")

    try:
        resp = requests.get(
            f"{base_url}/fetch-video",
            params={"post_url": post_url},
            headers=_remote_proxy_headers(token),
            timeout=300,
            stream=True,
        )
        resp.raise_for_status()

        # Get filename from Content-Disposition header or default
        cd = resp.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            filename = cd.split("filename=")[-1].strip().strip('"')
        else:
            content_type = resp.headers.get("Content-Type", "")
            ext_map = {
                "video/mp4": ".mp4",
                "video/webm": ".webm",
                "video/x-matroska": ".mkv",
            }
            ext = ext_map.get(content_type, ".mp4")
            filename = f"video_1{ext}"

        filepath = output_dir / filename
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        log.info(f"  Video downloaded via remote proxy: {filepath}")
        return [str(filepath)]

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        if status == 404:
            log.debug("  Remote proxy: no downloadable video")
        else:
            log.warning(f"  Remote proxy video fetch failed (HTTP {status}): {e}")
        return []
    except Exception as e:
        log.warning(f"  Remote proxy video fetch failed: {e}")
        return []


def download_images(
    image_urls: list[str],
    output_dir: Path,
    proxy_url: str = "",
    download_proxy: dict | None = None,
    delay_range: tuple[float, float] = (1.0, 4.0),
) -> list[str]:
    """
    Download images from Facebook CDN URLs.

    Args:
        proxy_url: SOCKS5 proxy URL for direct CDN fetches.
        download_proxy: Remote proxy config dict {"url": "...", "token": "..."}.
                        If set, images are fetched via the remote proxy server.

    Returns list of saved file paths.
    """
    if not requests:
        log.warning("Skipping image downloads (requests not installed)")
        return []

    saved = []
    output_dir.mkdir(parents=True, exist_ok=True)
    proxies = _get_proxy_dict(proxy_url)
    use_remote = download_proxy and download_proxy.get("url")

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

            # Random delay between downloads
            if i > 0:
                delay = random.uniform(*delay_range)
                log.debug(f"  Download delay: {delay:.1f}s")
                time.sleep(delay)

            if use_remote:
                log.info(f"  Downloading image {i + 1}/{len(image_urls)} via remote proxy")
                if _download_image_via_proxy(url, filepath, download_proxy):
                    saved.append(str(filepath))
                    log.info(f"  Saved: {filepath}")
            else:
                log.info(f"  Downloading image {i + 1}/{len(image_urls)}"
                         f"{' via SOCKS proxy' if proxy_url else ''}")
                resp = requests.get(url, timeout=30, proxies=proxies, headers={
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


def download_video_ytdlp(
    post_url: str,
    output_dir: Path,
    proxy_url: str = "",
    download_proxy: dict | None = None,
) -> list[str]:
    """
    Download video from a Facebook post using yt-dlp (locally or via remote proxy).

    Args:
        proxy_url: SOCKS5 proxy URL for local yt-dlp.
        download_proxy: Remote proxy config. If set, the remote server runs yt-dlp.

    Returns list of saved file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Use remote proxy if configured
    if download_proxy and download_proxy.get("url"):
        log.info("  Downloading video via remote proxy...")
        return _download_video_via_proxy(post_url, output_dir, download_proxy)

    # Otherwise run yt-dlp locally
    output_template = str(output_dir / "video_%(autonumber)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-check-certificates",
        "-o", output_template,
        "--no-playlist",
        "--restrict-filenames",
        "--no-overwrites",
    ]

    # Route through proxy if available
    if proxy_url:
        cmd.extend(["--proxy", proxy_url])
        log.info("  Downloading video via yt-dlp (through SOCKS proxy)...")
    else:
        log.info("  Downloading video via yt-dlp...")

    cmd.append(post_url)

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
                log.debug("  No downloadable video at this URL")
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
    proxy_url: str = "",
    download_proxy: dict | None = None,
    skip_downloads: bool = False,
) -> dict:
    """
    Download all attachments for a post.

    Args:
        proxy_url: SOCKS5 proxy URL (e.g. "socks5://127.0.0.1:9050")
        download_proxy: Remote proxy config {"url": "...", "token": "..."}.
                        If set, downloads go through the remote VPS proxy.
        skip_downloads: If True, record URLs but don't download files.

    Returns:
        {
            "images": [list of saved image paths],
            "videos": [list of saved video paths],
            "image_urls": [original URLs],
            "video_urls": [original URLs],
            "skipped": True/False,
        }
    """
    result = {
        "images": [],
        "videos": [],
        "image_urls": image_urls,
        "video_urls": video_urls,
        "skipped": skip_downloads,
    }

    if skip_downloads:
        log.info(f"  Skipping media downloads (urls-only mode): "
                 f"{len(image_urls)} images, {len(video_urls)} videos")
        return result

    attachments_dir = output_dir / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    mode = "remote proxy" if (download_proxy and download_proxy.get("url")) else \
           "SOCKS proxy" if proxy_url else "direct"

    # Download images
    if image_urls:
        result["images"] = download_images(
            image_urls, attachments_dir,
            proxy_url=proxy_url,
            download_proxy=download_proxy,
        )

    # Random delay between image and video downloads
    if image_urls and (video_urls or any(p in post_url for p in ("/videos/", "/watch/", "/reel/"))):
        time.sleep(random.uniform(2.0, 6.0))

    # Download videos — prefer yt-dlp on the post URL since it handles
    # Facebook's video player better than direct URLs
    is_video_post = any(p in post_url for p in ("/videos/", "/watch/", "/reel/"))
    if is_video_post or video_urls:
        result["videos"] = download_video_ytdlp(
            post_url, attachments_dir,
            proxy_url=proxy_url,
            download_proxy=download_proxy,
        )

    total = len(result["images"]) + len(result["videos"])
    if total > 0:
        log.info(f"  Downloaded {len(result['images'])} images, "
                 f"{len(result['videos'])} videos ({mode})")

    return result
