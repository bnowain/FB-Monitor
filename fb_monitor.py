#!/usr/bin/env python3
"""
Facebook Page Monitor

Monitors Facebook pages for new posts and captures:
- Post text, timestamps, shared source, links (as JSON)
- Attached images and videos (downloaded)
- Comments (tracked and updated for 24 hours)

Usage:
    python fb_monitor.py              # Run once (detect + comment recheck)
    python fb_monitor.py --watch      # Run continuously
    python fb_monitor.py --list       # List configured pages
    python fb_monitor.py --status     # Show tracking status
    python fb_monitor.py --health     # Show extractor health
    python fb_monitor.py --reset      # Clear all state
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("Playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    import requests as req_lib
except ImportError:
    req_lib = None

from extractors import extract_posts, get_health_report
from post_parser import parse_post
from comments import extract_comments, merge_comments, load_comments_file, save_comments_file
from downloader import download_attachments
from database import (
    init_db, save_post as db_save_post, save_comments as db_save_comments,
    save_attachments as db_save_attachments, queue_media_batch,
    get_pending_imports, update_import_status, get_post as db_get_post,
)
from tracker import (
    load_state, save_state, is_post_seen, mark_post_seen,
    add_tracking_job, get_due_tracking_jobs, update_tracking_job,
    prune_expired_jobs, get_tracking_summary,
)
from stealth import (
    jittered_interval, human_delay, human_scroll,
    create_stealth_context, stealth_goto, RateLimiter,
    get_tor_proxy, renew_tor_circuit,
)
from sessions import (
    interactive_login, create_session_context, get_account_for_page,
    group_pages_by_account, list_accounts, delete_account,
)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fb-monitor")


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def slugify(text: str) -> str:
    return re.sub(r'[^\w]+', '_', text.lower()).strip('_')


# ---------------------------------------------------------------------------
# Tor verification
# ---------------------------------------------------------------------------

def verify_tor_connection(config: dict) -> bool:
    """
    Verify the Tor SOCKS proxy is reachable by launching a headless browser
    through it and checking our IP via the Tor Project's check page.
    Returns True if traffic is routed through Tor.
    """
    proxy = get_tor_proxy(config)
    if not proxy:
        return False

    log.info("Verifying Tor connection...")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                proxy=proxy,
            )
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://check.torproject.org/api", timeout=30000)
            body = page.inner_text("body")
            page.close()
            context.close()
            browser.close()

            import json as _json
            result = _json.loads(body)
            is_tor = result.get("IsTor", False)
            ip = result.get("IP", "unknown")

            if is_tor:
                log.info(f"Tor connection verified — exit IP: {ip}")
            else:
                log.warning(f"Connected but NOT through Tor — IP: {ip}")

            return is_tor

    except Exception as e:
        log.error(f"Tor connection failed: {e}")
        log.error("Make sure Tor is running (e.g. 'sudo systemctl start tor' or 'tor &')")
        return False


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_notification(config: dict, page_name: str, post_url: str, message: str):
    notif = config.get("notifications", {})
    if not notif.get("enabled") or not req_lib:
        return

    text = f"📢 New post on {page_name}\n{message}\n{post_url}"

    webhook = notif.get("discord_webhook_url", "")
    if webhook:
        try:
            req_lib.post(webhook, json={"content": text}, timeout=10)
        except Exception as e:
            log.warning(f"Discord notification failed: {e}")

    ntfy = notif.get("ntfy_topic", "")
    if ntfy:
        try:
            req_lib.post(
                f"https://ntfy.sh/{ntfy}",
                data=text.encode("utf-8"),
                headers={"Title": f"New post: {page_name}"},
                timeout=10,
            )
        except Exception as e:
            log.warning(f"ntfy notification failed: {e}")


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

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


def open_post_page(browser_context, url: str, rate_limiter: RateLimiter = None, rotation_callback=None) -> "Page":
    """Open a post URL in a new page with stealth timing."""
    if rate_limiter:
        rate_limiter.wait_if_needed(rotation_callback=rotation_callback)

    page = browser_context.new_page()
    stealth_goto(page, url)
    _dismiss_dialogs(page)

    if rate_limiter:
        rate_limiter.record()

    return page


# ---------------------------------------------------------------------------
# Phase 1: Detect new posts
# ---------------------------------------------------------------------------

def detect_new_posts(page_configs: list[dict], config: dict, state: dict, browser_context, rate_limiter: RateLimiter, is_logged_in: bool = False) -> list[dict]:
    """
    Scan a list of pages for new posts.
    For each new post: parse data, download attachments, capture initial comments.
    """
    results = []

    # For anonymous+Tor sessions, provide a rotation callback so the rate
    # limiter can renew the circuit instead of blocking for a long time.
    tor_rotate = None
    if not is_logged_in and config.get("tor", {}).get("enabled"):
        tor_rotate = lambda: renew_tor_circuit(config)

    for page_cfg in page_configs:
        page_name = page_cfg["name"]
        page_url = page_cfg["url"]
        page_key = slugify(page_name)
        output_base = Path(config.get("output_dir", "downloads")) / page_key

        log.info(f"Checking page: {page_name}")

        # Rate limit check before loading feed
        rate_limiter.wait_if_needed(rotation_callback=tor_rotate)

        # Load the page feed
        feed_page = browser_context.new_page()

        try:
            stealth_goto(feed_page, page_url)
            _dismiss_dialogs(feed_page)
            rate_limiter.record()

            # Human-like scrolling to load posts
            human_scroll(feed_page, scroll_count=random.randint(2, 4))

        except PlaywrightTimeout:
            log.warning(f"Timeout loading {page_url}")
            feed_page.close()
            continue

        # Extract post links
        posts = extract_posts(feed_page, browser_context=browser_context, page_url=page_url)
        feed_page.close()

        if not posts:
            log.info(f"  No posts found on {page_name}")
            continue

        # Filter to new posts
        new_posts = [p for p in posts if not is_post_seen(state, page_key, p.id)]

        if not new_posts:
            log.info(f"  No new posts on {page_name}")
            continue

        log.info(f"  🆕 {len(new_posts)} new post(s)")

        for post in new_posts:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_id = re.sub(r'[^\w]', '_', post.id)[:50]
            post_dir = output_base / f"{timestamp}_{safe_id}"
            post_dir.mkdir(parents=True, exist_ok=True)

            log.info(f"  Processing: {post.url}")

            # --- Parse post data ---
            try:
                post_page = open_post_page(browser_context, post.url, rate_limiter, rotation_callback=tor_rotate)
                post_data = parse_post(
                    post_page,
                    browser_context=browser_context,
                    post_url=post.url,
                    post_id=post.id,
                    page_name=page_name,
                )
            except PlaywrightTimeout:
                log.warning(f"  Timeout loading post: {post.url}")
                post_data = None
                post_page = None
            except Exception as e:
                log.warning(f"  Failed to parse post: {e}")
                post_data = None
                post_page = None

            if not post_data:
                mark_post_seen(state, page_key, post.id)
                continue

            # --- Download attachments ---
            # Build proxy URL for media downloads (only for anonymous/Tor sessions)
            dl_proxy_url = ""
            if not is_logged_in:
                tor_proxy = get_tor_proxy(config)
                if tor_proxy:
                    dl_proxy_url = tor_proxy["server"]

            skip_downloads = config.get("skip_media_downloads", False)
            auto_dl_logged_in = config.get("auto_download_logged_in", False)

            # Remote download proxy (VPS) — used when configured
            dl_proxy_config = config.get("download_proxy")

            # For logged-in accounts: queue media for manual review unless forced
            is_gallery = len(post_data.image_urls) > 1

            if is_logged_in and not skip_downloads:
                account = get_account_for_page(page_cfg, config)

                # Galleries (multi-photo) ALWAYS queued on logged-in — only anon/Tor downloads those
                if is_gallery:
                    log.info(f"  Gallery post ({len(post_data.image_urls)} images) — "
                             f"queuing for manual download (logged-in account)")
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
                elif not auto_dl_logged_in:
                    # Single image or videos — queue unless auto-download forced
                    log.info(f"  Queuing {len(post_data.image_urls)} images, "
                             f"{len(post_data.video_urls)} videos for manual download")
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
                    # auto_download_logged_in=true AND not gallery — download directly
                    attachment_result = download_attachments(
                        post_url=post.url,
                        image_urls=post_data.image_urls,
                        video_urls=post_data.video_urls,
                        output_dir=post_dir,
                        proxy_url=dl_proxy_url,
                        download_proxy=dl_proxy_config,
                        skip_downloads=skip_downloads,
                    )
            else:
                attachment_result = download_attachments(
                    post_url=post.url,
                    image_urls=post_data.image_urls,
                    video_urls=post_data.video_urls,
                    output_dir=post_dir,
                    proxy_url=dl_proxy_url,
                    download_proxy=dl_proxy_config,
                    skip_downloads=skip_downloads,
                )

            # --- Save post.json ---
            post_json = post_data.to_dict()
            post_json["detected_at"] = datetime.now(timezone.utc).isoformat()
            post_json["attachments"] = attachment_result
            post_json["post_dir"] = str(post_dir)

            post_json_path = post_dir / "post.json"
            with open(post_json_path, "w", encoding="utf-8") as f:
                json.dump(post_json, f, indent=2, ensure_ascii=False)
            log.info(f"  Saved: {post_json_path}")

            # --- Write to database ---
            account = get_account_for_page(page_cfg, config)
            post_json["page_url"] = page_url
            db_save_post(post_json, account=account)
            db_save_attachments(post.id, attachment_result)

            # --- Initial comment capture ---
            initial_comments = []
            if post_page:
                try:
                    initial_comments = extract_comments(
                        post_page,
                        browser_context=browser_context,
                        post_url=post.url,
                    )
                    comments_path = post_dir / "comments.json"
                    save_comments_file(comments_path, initial_comments, post.url)
                    log.info(f"  Initial comments: {len(initial_comments)}")

                    # Write comments to DB
                    db_save_comments(post.id, [c.to_dict() for c in initial_comments])
                except Exception as e:
                    log.warning(f"  Initial comment extraction failed: {e}")

                post_page.close()

            # --- Register for comment tracking ---
            add_tracking_job(state, post.id, post.url, str(post_dir), page_name, account)

            # --- Mark seen ---
            mark_post_seen(state, page_key, post.id)

            # --- Notify ---
            preview = post_data.text[:120] if post_data.text else "(no text)"
            send_notification(config, page_name, post.url, preview)

            results.append(post_json)

            # Random delay between posts — much longer for logged-in accounts
            if post != new_posts[-1]:
                if is_logged_in:
                    lo, hi = config.get("logged_in_polling", {}).get(
                        "delay_between_posts_sec", [10, 30]
                    )
                    delay = random.uniform(lo, hi)
                else:
                    delay = human_delay(3.0, 8.0)
                log.debug(f"  Waiting {delay:.1f}s before next post...")
                time.sleep(delay)

    return results


# ---------------------------------------------------------------------------
# Phase 2: Recheck comments on tracked posts
# ---------------------------------------------------------------------------

def recheck_comments(due_jobs: list[dict], config: dict, state: dict, browser_context, rate_limiter: RateLimiter, is_logged_in: bool = False) -> int:
    """
    Recheck comments on the given tracking jobs.
    Returns total new comments found.
    """
    if not due_jobs:
        return 0

    log.info(f"Rechecking comments on {len(due_jobs)} post(s)...")
    total_new = 0

    tor_rotate = None
    if not is_logged_in and config.get("tor", {}).get("enabled"):
        tor_rotate = lambda: renew_tor_circuit(config)

    for job in due_jobs:
        post_url = job["post_url"]
        post_dir = Path(job["post_dir"])
        comments_path = post_dir / "comments.json"

        log.info(f"  Rechecking: {job['page_name']} — {job['post_id'][:30]}... "
                 f"(check #{job.get('comment_checks', 0) + 1})")

        try:
            post_page = open_post_page(browser_context, post_url, rate_limiter, rotation_callback=tor_rotate)
            new_comments = extract_comments(
                post_page,
                browser_context=browser_context,
                post_url=post_url,
            )
            post_page.close()
        except Exception as e:
            log.warning(f"  Failed to load post for comment recheck: {e}")
            update_tracking_job(state, job["post_id"])
            continue

        # Load existing and merge
        existing = load_comments_file(comments_path)
        merged, added = merge_comments(existing, new_comments)

        if added > 0:
            log.info(f"  +{added} new comment(s) (total: {len(merged)})")
            total_new += added
        else:
            log.info(f"  No new comments (total: {len(merged)})")

        # Save updated comments
        save_comments_file(comments_path, merged, post_url)
        db_save_comments(job["post_id"], [c.to_dict() for c in new_comments])
        update_tracking_job(state, job["post_id"])

        # Random delay between rechecks — longer for logged-in accounts
        if job != due_jobs[-1]:
            if is_logged_in:
                lo, hi = config.get("logged_in_polling", {}).get(
                    "delay_between_posts_sec", [10, 30]
                )
                delay = random.uniform(lo, hi)
            else:
                delay = human_delay(2.0, 6.0)
            time.sleep(delay)

    return total_new


# ---------------------------------------------------------------------------
# Phase 3: Process import queue (URL backfill)
# ---------------------------------------------------------------------------

def process_import_queue(config: dict, browser_context, rate_limiter: RateLimiter) -> int:
    """
    Process pending URLs from the import queue.
    Each URL is opened, parsed, and saved as if it were a newly discovered post.
    Uses the anonymous session. Returns count of successfully scraped posts.
    """
    pending = get_pending_imports(limit=20)
    if not pending:
        return 0

    log.info(f"Processing {len(pending)} imported URL(s)...")
    scraped = 0

    tor_rotate = None
    if config.get("tor", {}).get("enabled"):
        tor_rotate = lambda: renew_tor_circuit(config)

    for item in pending:
        url = item["url"]
        import_id = item["id"]
        page_name = item.get("page_name") or "Imported"

        # Derive a post_id from the URL
        post_id = re.sub(r'[^\w]', '_', url.split("facebook.com/")[-1] if "facebook.com/" in url else url)[:80]

        # Check if we already have this post
        existing = db_get_post(post_id)
        if existing:
            log.info(f"  Already have post: {post_id[:40]}...")
            update_import_status(import_id, "duplicate", post_id=post_id)
            continue

        page_key = slugify(page_name)
        output_base = Path(config.get("output_dir", "downloads")) / page_key
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_id = re.sub(r'[^\w]', '_', post_id)[:50]
        post_dir = output_base / f"{timestamp}_{safe_id}"
        post_dir.mkdir(parents=True, exist_ok=True)

        log.info(f"  Importing: {url}")

        try:
            post_page = open_post_page(browser_context, url, rate_limiter, rotation_callback=tor_rotate)
            post_data = parse_post(
                post_page,
                browser_context=browser_context,
                post_url=url,
                post_id=post_id,
                page_name=page_name,
            )
        except PlaywrightTimeout:
            log.warning(f"  Timeout loading import URL: {url}")
            update_import_status(import_id, "failed", error="Timeout loading page")
            continue
        except Exception as e:
            log.warning(f"  Failed to parse import URL: {e}")
            update_import_status(import_id, "failed", error=str(e)[:200])
            continue

        if not post_data:
            update_import_status(import_id, "failed", error="Could not parse post data")
            continue

        # Download attachments (anonymous, so direct download)
        dl_proxy_url = ""
        tor_proxy = get_tor_proxy(config)
        if tor_proxy:
            dl_proxy_url = tor_proxy["server"]

        skip_downloads = config.get("skip_media_downloads", False)
        dl_proxy_config = config.get("download_proxy")

        attachment_result = download_attachments(
            post_url=url,
            image_urls=post_data.image_urls,
            video_urls=post_data.video_urls,
            output_dir=post_dir,
            proxy_url=dl_proxy_url,
            download_proxy=dl_proxy_config,
            skip_downloads=skip_downloads,
        )

        # Save post.json
        post_json = post_data.to_dict()
        post_json["detected_at"] = datetime.now(timezone.utc).isoformat()
        post_json["attachments"] = attachment_result
        post_json["post_dir"] = str(post_dir)
        post_json["imported_from"] = url

        post_json_path = post_dir / "post.json"
        with open(post_json_path, "w", encoding="utf-8") as f:
            json.dump(post_json, f, indent=2, ensure_ascii=False)

        # Write to database
        post_json["page_url"] = ""
        db_save_post(post_json, account="anonymous")
        db_save_attachments(post_id, attachment_result)

        # Capture comments
        if post_page:
            try:
                initial_comments = extract_comments(
                    post_page,
                    browser_context=browser_context,
                    post_url=url,
                )
                comments_path = post_dir / "comments.json"
                save_comments_file(comments_path, initial_comments, url)
                db_save_comments(post_id, [c.to_dict() for c in initial_comments])
                log.info(f"  Comments: {len(initial_comments)}")
            except Exception as e:
                log.warning(f"  Comment extraction failed: {e}")
            post_page.close()

        update_import_status(import_id, "scraped", post_id=post_id)
        scraped += 1
        log.info(f"  Imported successfully: {post_id[:40]}...")

        # Random delay between imports
        if item != pending[-1]:
            delay = human_delay(3.0, 8.0)
            time.sleep(delay)

    return scraped


# ---------------------------------------------------------------------------
# Main run cycle
# ---------------------------------------------------------------------------

def _is_logged_in(account: str) -> bool:
    """True if this is a named (logged-in) account, not anonymous."""
    return account not in ("anonymous", "")


def _rotate_tor_session(pw, config: dict, old_context, old_browser, rate_limiter: RateLimiter):
    """
    Rotate the Tor circuit and create a fresh anonymous browser session.

    1. Sends SIGNAL NEWNYM to Tor (new exit node / IP)
    2. Closes the old browser context
    3. Creates a new one with a fresh fingerprint (user agent, viewport)
    4. Resets the rate limiter counter

    Returns (new_context, new_browser, success).
    If rotation fails, returns the old context/browser unchanged.
    """
    if not renew_tor_circuit(config):
        log.warning("  Tor rotation failed — continuing with current circuit")
        return old_context, old_browser, False

    # Close old session
    try:
        old_context.close()
        if old_browser:
            old_browser.close()
    except Exception:
        pass

    # Create fresh session with new fingerprint
    try:
        context, browser, _ = create_session_context(pw, "anonymous", config)
    except Exception as e:
        log.error(f"  Failed to create new session after Tor rotation: {e}")
        # Try to recreate with old settings as fallback
        context, browser, _ = create_session_context(pw, "anonymous", config)

    rate_limiter.reset()
    log.info(f"  New anonymous session ready (rate limiter reset: 0/{rate_limiter.max_per_hour})")
    return context, browser, True


def run_cycle(config: dict, anon_rate_limiter: RateLimiter, login_rate_limiter: RateLimiter):
    """
    Run one full cycle: detect new posts + recheck comments.

    Pages are grouped by account, and each account gets its own
    browser session (persistent profile for logged-in accounts,
    fresh stealth context for anonymous).

    Logged-in accounts use conservative timing to look like a
    real person casually browsing. Anonymous sessions use Tor and
    can poll more aggressively.
    """
    state = load_state()

    # Prune expired tracking jobs
    tracking_hours = config.get("comment_tracking_hours", 24)
    anon_recheck = config.get("comment_recheck_interval_minutes", 30)
    login_recheck = config.get("logged_in_polling", {}).get(
        "comment_recheck_interval_minutes", 90
    )
    prune_expired_jobs(state, tracking_hours)

    # Group pages by account
    account_groups = group_pages_by_account(config)

    # Group due comment-recheck jobs by account, using the right interval
    anon_due = get_due_tracking_jobs(state, anon_recheck, tracking_hours)
    login_due = get_due_tracking_jobs(state, login_recheck, tracking_hours)

    recheck_by_account: dict[str, list[dict]] = {}
    for job in anon_due:
        acct = job.get("account", "anonymous")
        if not _is_logged_in(acct):
            recheck_by_account.setdefault(acct, []).append(job)
    for job in login_due:
        acct = job.get("account", "anonymous")
        if _is_logged_in(acct):
            recheck_by_account.setdefault(acct, []).append(job)

    # Check if there are pending imports (processed by anonymous session)
    has_pending_imports = bool(get_pending_imports(limit=1))

    # Merge account lists (some accounts may only have rechecks, not new pages)
    all_accounts = set(account_groups.keys()) | set(recheck_by_account.keys())

    # Ensure anonymous is included if there are pending imports
    if has_pending_imports:
        all_accounts.add("anonymous")

    # Process anonymous accounts first (Tor), then logged-in (conservative)
    sorted_accounts = sorted(all_accounts, key=lambda a: (1 if _is_logged_in(a) else 0, a))

    all_new_posts = []
    total_new_comments = 0
    total_imports = 0
    logged_in_cfg = config.get("logged_in_polling", {})

    with sync_playwright() as pw:
        for account in sorted_accounts:
            pages_for_account = account_groups.get(account, [])
            rechecks_for_account = recheck_by_account.get(account, [])

            # For anonymous accounts, also check if there are imports to process
            should_process_imports = not _is_logged_in(account) and has_pending_imports
            if not pages_for_account and not rechecks_for_account and not should_process_imports:
                continue

            is_login = _is_logged_in(account)
            rate_limiter = login_rate_limiter if is_login else anon_rate_limiter
            mode_label = "conservative" if is_login else "anonymous/tor"

            log.info(f"{'=' * 40}")
            log.info(f"Account: {account} [{mode_label}] "
                     f"({len(pages_for_account)} page(s), "
                     f"{len(rechecks_for_account)} recheck(s))")

            # Create session for this account
            try:
                context, browser, is_temp = create_session_context(pw, account, config)
            except Exception as e:
                log.error(f"Failed to create session for '{account}': {e}")
                continue

            # Tor rotation is only available for anonymous sessions
            can_rotate = not is_login and config.get("tor", {}).get("enabled", False)

            try:
                # Phase 1: Detect new posts
                if pages_for_account:
                    # Conservative pre-delay for logged-in accounts
                    if is_login:
                        lo, hi = logged_in_cfg.get("delay_between_pages_sec", [15, 45])
                        pre = random.uniform(lo, hi)
                        log.info(f"  Waiting {pre:.0f}s (conservative pacing)...")
                        time.sleep(pre)

                    try:
                        new_posts = detect_new_posts(
                            pages_for_account, config, state, context,
                            rate_limiter, is_logged_in=is_login,
                        )
                        all_new_posts.extend(new_posts)
                    except Exception as e:
                        log.error(f"Error detecting posts for '{account}': {e}")

                # Between phases: rotate Tor if rate limited
                wait_needed = rate_limiter.should_wait()
                if can_rotate and wait_needed and wait_needed > 60:
                    log.info(f"  Rate limit approaching — rotating Tor circuit instead of waiting {wait_needed:.0f}s")
                    context, browser, rotated = _rotate_tor_session(
                        pw, config, context, browser, rate_limiter
                    )
                    is_temp = True  # new browser needs cleanup
                elif is_login:
                    time.sleep(human_delay(10.0, 30.0))
                else:
                    time.sleep(human_delay(2.0, 5.0))

                # Phase 2: Recheck comments
                if rechecks_for_account:
                    try:
                        new_comments = recheck_comments(
                            rechecks_for_account, config, state, context,
                            rate_limiter, is_logged_in=is_login,
                        )
                        total_new_comments += new_comments
                    except Exception as e:
                        log.error(f"Error rechecking comments for '{account}': {e}")

                # Between phases: rotate Tor if rate limited again
                wait_needed = rate_limiter.should_wait()
                if can_rotate and wait_needed and wait_needed > 60:
                    log.info(f"  Rate limit approaching — rotating Tor circuit instead of waiting {wait_needed:.0f}s")
                    context, browser, rotated = _rotate_tor_session(
                        pw, config, context, browser, rate_limiter
                    )
                    is_temp = True
                else:
                    time.sleep(human_delay(2.0, 5.0))

                # Phase 3: Process import queue (anonymous only)
                if should_process_imports:
                    try:
                        imported = process_import_queue(config, context, rate_limiter)
                        total_imports += imported
                    except Exception as e:
                        log.error(f"Error processing imports: {e}")

            finally:
                # Clean up this account's session
                try:
                    context.close()
                    if browser and is_temp:
                        browser.close()
                except Exception:
                    pass

            # Delay between accounts — longer for logged-in
            if account != sorted_accounts[-1]:
                if is_login:
                    delay = human_delay(15.0, 45.0)
                else:
                    delay = human_delay(3.0, 8.0)
                log.debug(f"Waiting {delay:.1f}s before next account...")
                time.sleep(delay)

    save_state(state)

    return all_new_posts, total_new_comments, total_imports


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Facebook Page Monitor")
    parser.add_argument("--watch", action="store_true", help="Run continuously")
    parser.add_argument("--list", action="store_true", help="List configured pages")
    parser.add_argument("--status", action="store_true", help="Show tracking status")
    parser.add_argument("--health", action="store_true", help="Show extractor health")
    parser.add_argument("--reset", action="store_true", help="Clear all state")
    parser.add_argument("--login", type=str, metavar="ACCOUNT", help="Log in to an account (opens browser)")
    parser.add_argument("--accounts", action="store_true", help="List saved account sessions")
    parser.add_argument("--logout", type=str, metavar="ACCOUNT", help="Delete a saved account session")
    parser.add_argument("--deep-scrape", type=str, metavar="PAGE_NAME", help="Deep scrape a page's full history with logged-in session")
    parser.add_argument("--max-posts", type=int, default=0, help="Max posts to process in deep scrape (0=unlimited)")
    parser.add_argument("--account", type=str, default="", help="Account to use for deep scrape")
    parser.add_argument("--tor", action="store_true", help="Route all traffic through Tor (SOCKS5 on 127.0.0.1:9050)")
    parser.add_argument("--config", type=str, default=str(CONFIG_PATH), help="Path to config file")
    args = parser.parse_args()

    global CONFIG_PATH
    CONFIG_PATH = Path(args.config)

    if not CONFIG_PATH.exists():
        log.error(f"Config not found: {CONFIG_PATH}")
        sys.exit(1)

    config = load_config()
    init_db()

    # --- Tor override from CLI ---
    if args.tor:
        config.setdefault("tor", {})
        config["tor"]["enabled"] = True
        config["tor"].setdefault("socks_port", 9050)

    # --- Account management (no config needed) ---

    if args.login:
        interactive_login(args.login)
        return

    if args.accounts:
        saved = list_accounts()
        if saved:
            print("\nSaved account sessions:")
            for name in saved:
                print(f"  ✅ {name}")
        else:
            print("\nNo saved sessions. Use --login <name> to set one up.")

        # Show which accounts are referenced in config
        referenced = set()
        default = config.get("default_account", "")
        if default:
            referenced.add(default)
        for p in config.get("pages", []):
            acct = p.get("account", "")
            if acct:
                referenced.add(acct)

        missing = referenced - set(saved) - {"anonymous"}
        if missing:
            print(f"\n  ⚠️  Referenced in config but not logged in: {', '.join(missing)}")
            print(f"     Run: python fb_monitor.py --login <name>")
        return

    if args.logout:
        if delete_account(args.logout):
            print(f"Deleted session for '{args.logout}'")
        else:
            print(f"No session found for '{args.logout}'")
        return

    if args.list:
        default_acct = config.get("default_account", "anonymous")
        print(f"\nDefault account: {default_acct}")
        print(f"\nConfigured pages:")
        for i, p in enumerate(config.get("pages", []), 1):
            status = "✅" if p.get("enabled", True) else "❌"
            acct = p.get("account", default_acct)
            print(f"  {i}. {status} {p['name']}")
            print(f"     {p['url']}")
            print(f"     Account: {acct}")
        return

    if args.status:
        state = load_state()
        tracking_hours = config.get("comment_tracking_hours", 24)
        print(f"\n{get_tracking_summary(state, tracking_hours)}")
        return

    if args.health:
        print(f"\n{get_health_report()}")
        return

    if args.reset:
        state_file = BASE_DIR / "state.json"
        health_file = BASE_DIR / "extractor_health.json"
        for f in (state_file, health_file):
            if f.exists():
                f.unlink()
                print(f"Removed: {f}")
        print("State cleared.")
        return

    # --- Deep scrape mode ---
    if args.deep_scrape:
        from deep_scrape import deep_scrape_page

        target_name = args.deep_scrape

        # Find the page in config
        page_cfg = None
        for p in config.get("pages", []):
            if p["name"].lower() == target_name.lower():
                page_cfg = p
                break

        if not page_cfg:
            # Allow using a URL directly
            if "facebook.com/" in target_name:
                page_cfg = {"name": target_name.split("/")[-1], "url": target_name}
            else:
                log.error(f"Page '{target_name}' not found in config. Available pages:")
                for p in config.get("pages", []):
                    print(f"  - {p['name']}")
                sys.exit(1)

        # Determine which account to use
        account = args.account
        if not account:
            account = page_cfg.get("account", "") or config.get("default_account", "anonymous")

        if account == "anonymous":
            log.warning("Deep scrape works best with a logged-in account.")
            log.warning("Use --account <name> or set default_account in config.json")

        deep_scrape_page(
            page_name=page_cfg["name"],
            page_url=page_cfg["url"],
            account=account,
            config=config,
            max_posts=args.max_posts,
        )
        return

    # --- Verify Tor connection if enabled ---
    if config.get("tor", {}).get("enabled", False):
        if not verify_tor_connection(config):
            log.error("Aborting: Tor is enabled but connection could not be verified.")
            sys.exit(1)

    # Build rate limiters — separate limits for anonymous vs logged-in
    anon_max = config.get("max_requests_per_hour", 30)
    login_max = config.get("logged_in_polling", {}).get("max_requests_per_hour", 8)
    anon_rate_limiter = RateLimiter(max_per_hour=anon_max)
    login_rate_limiter = RateLimiter(max_per_hour=login_max)

    if args.watch:
        interval = config.get("check_interval_minutes", 15)

        log.info(f"Watch mode — base interval: {interval}min (±40% jitter)")
        log.info(f"Comment tracking: {config.get('comment_tracking_hours', 24)}h window")
        log.info(f"Rate limits: anonymous={anon_max}/hr, logged-in={login_max}/hr")
        if config.get("tor", {}).get("enabled"):
            log.info("Tor: enabled for anonymous sessions only")

        while True:
            try:
                new_posts, new_comments, imports = run_cycle(
                    config, anon_rate_limiter, login_rate_limiter
                )

                # Jittered sleep — never the same interval twice
                sleep_secs = jittered_interval(interval)
                sleep_mins = sleep_secs / 60
                log.info(
                    f"Cycle complete: {len(new_posts)} new post(s), "
                    f"{new_comments} new comment(s), "
                    f"{imports} imported. "
                    f"Next check in {sleep_mins:.1f}min "
                    f"(anon: {anon_rate_limiter.count_last_hour()}/{anon_max}/hr, "
                    f"login: {login_rate_limiter.count_last_hour()}/{login_max}/hr)"
                )
                time.sleep(sleep_secs)
            except KeyboardInterrupt:
                log.info("Stopped.")
                break
    else:
        new_posts, new_comments, imports = run_cycle(
            config, anon_rate_limiter, login_rate_limiter
        )
        print(f"\n{'=' * 50}")
        print(f"Results: {len(new_posts)} new post(s), {new_comments} new comment(s), {imports} imported")

        if new_posts:
            print(f"\nNew posts:")
            for p in new_posts:
                text = p.get("text", "")[:80]
                shared = f" (shared from {p['shared_from']})" if p.get("shared_from") else ""
                imgs = len(p.get("attachments", {}).get("images", []))
                vids = len(p.get("attachments", {}).get("videos", []))
                print(f"  📄 {p.get('page_name', '?')}{shared}")
                print(f"     {text}...")
                print(f"     {p['url']}")
                if imgs:
                    print(f"     🖼  {imgs} image(s)")
                if vids:
                    print(f"     🎬 {vids} video(s)")
                print(f"     📁 {p.get('post_dir', '')}")
                print()


if __name__ == "__main__":
    main()
