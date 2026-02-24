"""
scraper_status.py — Lightweight scraper activity broadcaster.

The scraper writes status updates to a JSON file. The web UI reads it
via an API endpoint and renders a live status bar.

Usage (in fb_monitor.py):
    from scraper_status import status
    status.scraping_page("Vote Kevin Crye", 3, 24)
    status.page_done(posts_found=5, new_posts=2)
    status.downloading_media("Vote Kevin Crye", images=3, videos=1)
    status.rechecking_comments(8, 25)
    status.waiting(next_poll_secs=180)
    status.idle()
"""

import json
import time
from pathlib import Path

STATUS_FILE = Path(__file__).parent / "scraper_status.json"

# How stale (seconds) before the UI considers the scraper offline
# Scraping a single page can take 60-90s through Tor, so allow generous staleness
STALE_THRESHOLD = 300


class ScraperStatus:
    """Thread-safe status broadcaster (single writer, multiple readers)."""

    def __init__(self):
        self._data = {
            "state": "starting",
            "message": "Starting up...",
            "page": None,
            "page_progress": None,     # [current, total]
            "cycle": 0,
            "cycle_stats": {
                "posts_found": 0,
                "posts_new": 0,
                "images": 0,
                "videos": 0,
                "comments_rechecked": 0,
                "pages_checked": 0,
                "pages_total": 0,
            },
            "tor": {
                "healthy": 0,
                "total": 0,
                "login_walls": 0,
            },
            "next_poll_at": None,      # epoch
            "updated_at": time.time(),
            "started_at": time.time(),
        }
        # Don't flush on construction — only the scraper process
        # should write; web_ui imports this module for read() only.

    def _flush(self):
        self._data["updated_at"] = time.time()
        try:
            STATUS_FILE.write_text(json.dumps(self._data, indent=2))
        except Exception:
            pass

    # --- State transitions ---

    def starting(self, message: str = "Starting up..."):
        self._data["state"] = "starting"
        self._data["message"] = message
        self._flush()

    def scraping_page(self, page_name: str, current: int, total: int):
        self._data["state"] = "scraping"
        self._data["page"] = page_name
        self._data["page_progress"] = [current, total]
        self._data["message"] = f"Checking {page_name}"
        self._data["cycle_stats"]["pages_total"] = total
        self._flush()

    def page_done(self, posts_found: int = 0, new_posts: int = 0,
                  images: int = 0, videos: int = 0):
        self._data["cycle_stats"]["pages_checked"] += 1
        self._data["cycle_stats"]["posts_found"] += posts_found
        self._data["cycle_stats"]["posts_new"] += new_posts
        self._data["cycle_stats"]["images"] += images
        self._data["cycle_stats"]["videos"] += videos
        self._flush()

    def downloading_media(self, page_name: str, images: int = 0, videos: int = 0):
        self._data["state"] = "downloading"
        self._data["message"] = f"Downloading media from {page_name}"
        self._flush()

    def rechecking_comments(self, current: int, total: int):
        self._data["state"] = "rechecking"
        self._data["message"] = f"Rechecking comments ({current}/{total})"
        self._data["cycle_stats"]["comments_rechecked"] = current
        self._flush()

    def processing_imports(self, count: int):
        self._data["state"] = "importing"
        self._data["message"] = f"Processing {count} import(s)"
        self._flush()

    def waiting(self, next_poll_secs: float):
        self._data["state"] = "waiting"
        self._data["next_poll_at"] = time.time() + next_poll_secs
        mins = next_poll_secs / 60
        self._data["message"] = f"Next poll in {mins:.0f}min"
        self._data["page"] = None
        self._data["page_progress"] = None
        self._flush()

    def idle(self):
        self._data["state"] = "idle"
        self._data["message"] = "Idle"
        self._data["page"] = None
        self._data["page_progress"] = None
        self._flush()

    def cycle_start(self, cycle_num: int):
        self._data["cycle"] = cycle_num
        self._data["cycle_stats"] = {
            "posts_found": 0,
            "posts_new": 0,
            "images": 0,
            "videos": 0,
            "comments_rechecked": 0,
            "pages_checked": 0,
            "pages_total": 0,
        }
        self._flush()

    def update_tor(self, healthy: int, total: int, login_walls: int = 0):
        self._data["tor"] = {
            "healthy": healthy,
            "total": total,
            "login_walls": login_walls,
        }
        self._flush()

    def error(self, message: str):
        self._data["state"] = "error"
        self._data["message"] = message
        self._flush()

    @staticmethod
    def read() -> dict:
        """Read current status from disk (used by web UI)."""
        try:
            if STATUS_FILE.exists():
                data = json.loads(STATUS_FILE.read_text())
                # Mark as offline if stale
                age = time.time() - data.get("updated_at", 0)
                data["online"] = age < STALE_THRESHOLD
                data["age_seconds"] = round(age)
                return data
        except Exception:
            pass
        return {
            "state": "offline",
            "message": "Scraper not running",
            "online": False,
            "age_seconds": 0,
        }


# Singleton — import and use directly
status = ScraperStatus()
