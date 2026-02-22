"""
deep_scrape.py — Deep scrape a Facebook page using a logged-in session.

Opens a page, scrolls to load all historical posts, then clicks into
each one to extract full text, all comments (expanded), and media URLs.
Designed for one-time backfill of a page's entire post history.

Usage (via fb_monitor.py):
    python fb_monitor.py --deep-scrape "Vote Kevin Crye"
    python fb_monitor.py --deep-scrape "Vote Kevin Crye" --max-posts 50
    python fb_monitor.py --deep-scrape "Vote Kevin Crye" --account myaccount
"""

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from extractors import extract_posts, _extract_post_id
from post_parser import parse_post
from comments import extract_comments, save_comments_file
from downloader import download_attachments
from database import (
    init_db, save_post as db_save_post, save_comments as db_save_comments,
    save_attachments as db_save_attachments, get_post as db_get_post,
    queue_media_batch,
)
from sessions import create_session_context, get_profile_dir
from stealth import human_delay, stealth_goto, human_scroll_delay
from tracker import load_state, save_state, mark_post_seen, slugify

log = logging.getLogger("fb-monitor")


def _deep_scroll(page, max_scrolls: int = 100, no_new_threshold: int = 5):
    """
    Scroll aggressively to load all posts on a page.

    Keeps scrolling until no new content appears for several consecutive
    scrolls, or until max_scrolls is reached.
    """
    last_height = 0
    no_new_count = 0

    for i in range(max_scrolls):
        # Scroll to bottom
        current_height = page.evaluate("document.body.scrollHeight")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # Wait for content to load
        delay = random.uniform(1.5, 3.5)
        page.wait_for_timeout(int(delay * 1000))

        new_height = page.evaluate("document.body.scrollHeight")

        if new_height == current_height:
            no_new_count += 1
            if no_new_count >= no_new_threshold:
                log.info(f"  Scroll complete after {i + 1} scrolls (no new content)")
                break
            # Try clicking "See more posts" or similar buttons
            for selector in [
                'div[role="button"]:has-text("See more")',
                'div[role="button"]:has-text("See More")',
            ]:
                try:
                    btn = page.query_selector(selector)
                    if btn and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(2000)
                        no_new_count = 0
                except Exception:
                    pass
        else:
            no_new_count = 0

        if (i + 1) % 10 == 0:
            log.info(f"  Scrolled {i + 1} times...")

        last_height = new_height

    return last_height


def _dismiss_dialogs(page):
    """Close login/cookie popups."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass

    for sel in [
        '[aria-label="Close"]',
        '[data-testid="cookie-policy-manage-dialog-accept-button"]',
        'div[role="dialog"] [aria-label="Close"]',
    ]:
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                page.wait_for_timeout(500)
        except Exception:
            pass


def deep_scrape_page(
    page_name: str,
    page_url: str,
    account: str,
    config: dict,
    max_posts: int = 0,
    skip_existing: bool = True,
):
    """
    Deep scrape a Facebook page: load all posts, extract everything.

    Args:
        page_name: Display name for the page
        page_url: Facebook page URL
        account: Account name to use (must have a saved session)
        config: Config dict
        max_posts: Max posts to process (0 = unlimited)
        skip_existing: Skip posts already in the database
    """
    output_dir = Path(config.get("output_dir", "downloads"))
    page_key = slugify(page_name)
    output_base = output_dir / page_key
    state = load_state()

    log.info(f"Deep scrape: {page_name}")
    log.info(f"  URL: {page_url}")
    log.info(f"  Account: {account}")
    if max_posts:
        log.info(f"  Max posts: {max_posts}")

    with sync_playwright() as pw:
        # Create browser session
        context, browser, is_temp = create_session_context(pw, account, config)

        try:
            # Phase 1: Load the page and scroll to collect all post URLs
            log.info("Phase 1: Loading page and collecting post URLs...")
            feed_page = context.new_page()

            try:
                stealth_goto(feed_page, page_url, timeout=60000)
                _dismiss_dialogs(feed_page)
            except PlaywrightTimeout:
                log.error(f"Timeout loading page: {page_url}")
                return

            # Aggressive scroll to load all historical posts
            _deep_scroll(feed_page, max_scrolls=200)

            # Extract all post URLs
            posts = extract_posts(feed_page, browser_context=context, page_url=page_url)
            feed_page.close()

            if not posts:
                log.warning("No posts found on page")
                return

            log.info(f"Found {len(posts)} post(s) on page")

            # Filter out already-scraped posts
            if skip_existing:
                original_count = len(posts)
                posts = [
                    p for p in posts
                    if not db_get_post(p.id)
                ]
                skipped = original_count - len(posts)
                if skipped:
                    log.info(f"  Skipping {skipped} posts already in database")

            if not posts:
                log.info("All posts already scraped")
                return

            # Apply max_posts limit
            if max_posts and len(posts) > max_posts:
                posts = posts[:max_posts]
                log.info(f"  Limited to {max_posts} posts")

            log.info(f"Processing {len(posts)} post(s)...")

            # Phase 2: Process each post
            processed = 0
            failed = 0

            for i, post in enumerate(posts, 1):
                log.info(f"[{i}/{len(posts)}] {post.url}")

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_id = re.sub(r'[^\w]', '_', post.id)[:50]
                post_dir = output_base / f"{timestamp}_{safe_id}"
                post_dir.mkdir(parents=True, exist_ok=True)

                # Open post page
                try:
                    post_page = context.new_page()
                    stealth_goto(post_page, post.url, timeout=45000)
                    _dismiss_dialogs(post_page)
                except PlaywrightTimeout:
                    log.warning(f"  Timeout loading post")
                    failed += 1
                    try:
                        post_page.close()
                    except Exception:
                        pass
                    continue
                except Exception as e:
                    log.warning(f"  Failed to load post: {e}")
                    failed += 1
                    try:
                        post_page.close()
                    except Exception:
                        pass
                    continue

                # Parse post data
                try:
                    post_data = parse_post(
                        post_page,
                        browser_context=context,
                        post_url=post.url,
                        post_id=post.id,
                        page_name=page_name,
                    )
                except Exception as e:
                    log.warning(f"  Failed to parse post: {e}")
                    post_data = None

                if not post_data:
                    log.warning(f"  Could not extract post data, skipping")
                    post_page.close()
                    failed += 1
                    continue

                # Download attachments — queue media for logged-in sessions
                dl_proxy_config = config.get("download_proxy")
                skip_downloads = config.get("skip_media_downloads", False)

                if account != "anonymous" and not config.get("auto_download_logged_in", False):
                    # Queue for manual download
                    if post_data.image_urls or post_data.video_urls:
                        queue_media_batch(
                            post.id,
                            post_data.image_urls,
                            post_data.video_urls,
                            post_url=post.url,
                            account=account,
                        )
                    attachment_result = {
                        "images": [], "videos": [],
                        "image_urls": post_data.image_urls,
                        "video_urls": post_data.video_urls,
                        "skipped": False, "queued": True,
                    }
                else:
                    attachment_result = download_attachments(
                        post_url=post.url,
                        image_urls=post_data.image_urls,
                        video_urls=post_data.video_urls,
                        output_dir=post_dir,
                        download_proxy=dl_proxy_config,
                        skip_downloads=skip_downloads,
                    )

                # Extract comments (with expansion)
                comments = []
                try:
                    comments = extract_comments(
                        post_page,
                        browser_context=context,
                        post_url=post.url,
                    )
                    log.info(f"  Text: {len(post_data.text or '')} chars, "
                             f"Comments: {len(comments)}, "
                             f"Images: {len(post_data.image_urls)}, "
                             f"Videos: {len(post_data.video_urls)}")
                except Exception as e:
                    log.warning(f"  Comment extraction failed: {e}")

                post_page.close()

                # Save post.json
                post_json = post_data.to_dict()
                post_json["detected_at"] = datetime.now(timezone.utc).isoformat()
                post_json["attachments"] = attachment_result
                post_json["post_dir"] = str(post_dir)
                post_json["deep_scraped"] = True

                post_json_path = post_dir / "post.json"
                with open(post_json_path, "w", encoding="utf-8") as f:
                    json.dump(post_json, f, indent=2, ensure_ascii=False)

                # Save comments
                if comments:
                    comments_path = post_dir / "comments.json"
                    save_comments_file(comments_path, comments, post.url)

                # Write to database
                post_json["page_url"] = page_url
                db_save_post(post_json, account=account)
                db_save_attachments(post.id, attachment_result)
                if comments:
                    db_save_comments(post.id, [c.to_dict() for c in comments])

                # Mark as seen
                mark_post_seen(state, page_key, post.id)

                processed += 1

                # Human-like delay between posts
                if i < len(posts):
                    delay = random.uniform(5.0, 15.0)
                    time.sleep(delay)

            save_state(state)

            log.info(f"\nDeep scrape complete: {page_name}")
            log.info(f"  Processed: {processed}")
            log.info(f"  Failed: {failed}")
            log.info(f"  Output: {output_base}")

        finally:
            try:
                context.close()
                if browser:
                    browser.close()
            except Exception:
                pass
