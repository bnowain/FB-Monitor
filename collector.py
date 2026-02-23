"""
collector.py — Python bridge between Playwright and the injected JS collector.

Injects injected_collector.js into a Playwright page, then orchestrates the
5-phase extraction pipeline:
  1. Open comment sections
  2. Switch to "All comments"
  3. Expand threads (batched)
  4. Extract posts + comments
  5. Capture images via canvas

All cleaning/filtering happens Python-side via sanitize.py.
"""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright._impl._errors import TargetClosedError

from sanitize import sanitize_post, sanitize_comments

log = logging.getLogger("fb-monitor")

JS_PATH = Path(__file__).parent / "injected_collector.js"


def _clean_posts(raw_posts: list[dict], page_name: str, page_url: str) -> list[dict]:
    """Apply sanitize_post/sanitize_comments to raw post dicts from JS."""
    now = datetime.now(timezone.utc).isoformat()
    cleaned = []

    for raw in raw_posts:
        post_data = {
            "post_id": raw.get("post_id", ""),
            "page_name": page_name,
            "page_url": page_url,
            "url": raw.get("post_url", ""),
            "author": raw.get("author", ""),
            "text": raw.get("text", ""),
            "timestamp": raw.get("timestamp", ""),
            "timestamp_raw": raw.get("timestamp", ""),
            "shared_from": raw.get("shared_from", ""),
            "shared_original_url": "",
            "links": raw.get("links", []),
            "reaction_count": raw.get("reaction_count", ""),
            "comment_count_text": raw.get("comment_count_text", ""),
            "share_count_text": raw.get("share_count_text", ""),
            "image_urls": raw.get("image_urls", []),
            "image_data": raw.get("image_data", []),
            "video_urls": raw.get("video_urls", []),
            "detected_at": now,
        }

        post_data = sanitize_post(post_data, page_name)
        if post_data is None:
            continue

        raw_comments = raw.get("comments", [])
        post_data["comments"] = sanitize_comments(raw_comments, page_name)
        cleaned.append(post_data)

    return cleaned


def inject(page) -> bool:
    """
    Read and evaluate injected_collector.js, then verify window.__fbm exists.
    Returns True on success.
    """
    js_code = JS_PATH.read_text(encoding="utf-8")
    page.evaluate(js_code)

    # Verify the API object was created
    ok = page.evaluate("() => typeof window.__fbm === 'object' && window.__fbm !== null")
    if not ok:
        log.error("Injection failed: window.__fbm not found")
        return False

    log.info("Injected collector JS — window.__fbm ready")
    return True


def expand_and_extract(
    page,
    page_name: str = "",
    page_url: str = "",
    capture_images: bool = False,
    expand_rounds_per_batch: int = 10,
    max_total_rounds: int = 50,
) -> list[dict]:
    """
    Orchestrate the 5-phase extraction pipeline.

    Args:
        page: Playwright page with injected_collector.js already loaded.
        page_name: Page name for sanitization (auto-detected if empty).
        page_url: Page URL to store on posts (defaults to page.url).
        capture_images: Whether to run canvas image capture (Phase 5).
        expand_rounds_per_batch: How many expand rounds per evaluate call.
        max_total_rounds: Stop expanding after this many total rounds.

    Returns:
        List of post dicts ready for db.save_post(), with comments already
        cleaned and attached as post["comments"].
    """
    if not page_url:
        page_url = page.url.split("?")[0]

    # Auto-detect page name from the DOM if not provided
    if not page_name:
        page_name = page.evaluate("() => window.__fbm.getPageName()") or ""
        if page_name:
            log.info(f"  Detected page name: {page_name}")

    def _safe_evaluate(expression, description="evaluate"):
        """Run page.evaluate, returning None if the page died (navigation/close)."""
        try:
            return page.evaluate(expression)
        except TargetClosedError:
            log.warning(f"    Page closed during {description} — skipping remaining phases")
            return None
        except Exception as e:
            if "navigation" in str(e).lower() or "destroyed" in str(e).lower():
                log.warning(f"    Page navigated during {description} — skipping")
                return None
            raise

    # --- Phase 1: Open comment sections ---
    log.info("  Phase 1: Opening comment sections...")
    result = _safe_evaluate("() => window.__fbm.openCommentSections()", "open comments")
    if result is None:
        return _clean_posts([], page_name, page_url)
    log.info(f"    Clicked {result.get('clicked', 0)} comment buttons")
    if result.get("clicked", 0) > 0:
        page.wait_for_timeout(2000)

    # --- Phase 2: Switch to "All comments" ---
    log.info("  Phase 2: Switching to All Comments...")
    result = _safe_evaluate("() => window.__fbm.switchToAllComments()", "switch filter")
    if result is None:
        return _clean_posts([], page_name, page_url)
    log.info(f"    Switched {result.get('switched', 0)} filters")
    if result.get("switched", 0) > 0:
        page.wait_for_timeout(2000)

    # --- Phase 3: Expand threads (batched) ---
    log.info("  Phase 3: Expanding threads...")
    total_expanded = 0
    total_rounds = 0
    page_alive = True

    while total_rounds < max_total_rounds and page_alive:
        batch_size = min(expand_rounds_per_batch, max_total_rounds - total_rounds)
        result = _safe_evaluate(
            f"() => window.__fbm.expandThreads({{ maxRounds: {batch_size} }})",
            "expand threads",
        )
        if result is None:
            page_alive = False
            break

        clicked = result.get("clicked", 0)
        rounds = result.get("rounds", 0)
        remaining = result.get("remaining", 0)

        total_expanded += clicked
        total_rounds += rounds

        log.info(f"    Batch: {clicked} clicks in {rounds} rounds, {remaining} buttons remaining")

        if remaining == 0 or clicked == 0:
            break

        # Brief pause between batches
        page.wait_for_timeout(1000)

    log.info(f"    Total expanded: {total_expanded} clicks in {total_rounds} rounds")

    if not page_alive:
        return _clean_posts([], page_name, page_url)

    # --- Phase 4: Extract posts + comments ---
    log.info("  Phase 4: Extracting posts...")
    if capture_images:
        # Phase 4+5 combined: extract and capture in one evaluate
        log.info("  Phase 5: Capturing images...")
        result = _safe_evaluate("""() => {
            const posts = window.__fbm.extractPosts();
            return window.__fbm.captureImages(posts);
        }""", "extract+capture")
        if result is None:
            return _clean_posts([], page_name, page_url)
        raw_posts = result.get("posts", [])
        log.info(f"    Found {len(raw_posts)} raw posts")
        log.info(f"    Captured {result.get('captured', 0)}/{result.get('total', 0)} images")
    else:
        raw_posts = _safe_evaluate("() => window.__fbm.extractPosts()", "extract")
        if raw_posts is None:
            return _clean_posts([], page_name, page_url)
        log.info(f"    Found {len(raw_posts)} raw posts")

    # --- Python-side cleaning ---
    cleaned_posts = _clean_posts(raw_posts, page_name, page_url)

    log.info(f"  Result: {len(cleaned_posts)} posts after cleaning "
             f"({len(raw_posts) - len(cleaned_posts)} rejected)")

    total_comments = sum(len(p["comments"]) for p in cleaned_posts)
    log.info(f"  Total comments: {total_comments}")

    return cleaned_posts
