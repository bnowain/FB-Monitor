#!/usr/bin/env python3
"""
test_extract.py — Standalone test for feed-page extraction via injected collector.

Usage:
    python test_extract.py "https://www.facebook.com/votekevincrye"
    python test_extract.py "https://www.facebook.com/votekevincrye" --save
    python test_extract.py "https://www.facebook.com/votekevincrye" --headless
    python test_extract.py "https://www.facebook.com/votekevincrye" --save --headless --images
    python test_extract.py "https://www.facebook.com/votekevincrye" --no-tor

Options:
    --save       Write extracted posts to the database
    --headless   Run browser in headless mode (default: visible)
    --images     Capture images from DOM via canvas
    --json       Save raw extraction to extract_output.json
    --no-tor     Skip Tor proxy (direct connection, exposes your IP)
    --retries N  Max Tor circuit retries on login wall (default: 5)
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from collector import inject, expand_and_extract
from sanitize import is_login_wall
from sessions import create_session_context
from stealth import human_scroll, renew_tor_circuit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fb-monitor")

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    """Load config.json, return empty dict on failure."""
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def check_login_wall(page) -> bool:
    """Check if the current page is a Facebook login wall."""
    try:
        body_text = page.evaluate("() => document.body.innerText.substring(0, 1000)")
        return is_login_wall(body_text)
    except Exception:
        return False


def navigate_and_check(pw, config, url, max_retries=5):
    """
    Create anonymous session, navigate to URL, detect login wall.
    On login wall: rotate Tor circuit, get fresh context, retry.

    Returns (page, context, browser, needs_close) on success, or exits on failure.
    """
    using_tor = config.get("tor", {}).get("enabled", False)

    for attempt in range(1, max_retries + 1):
        context, browser, needs_close = create_session_context(
            pw, "anonymous", config
        )
        page = context.new_page()

        print(f"  Attempt {attempt}/{max_retries}: Navigating to {url}...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
        except Exception as e:
            print(f"  Navigation failed: {e}")
            _close_session(context, browser, needs_close)
            if using_tor and attempt < max_retries:
                _rotate_and_wait(config, attempt)
                continue
            break

        if check_login_wall(page):
            current_url = page.url
            print(f"  Login wall detected (redirected to {current_url})")
            _close_session(context, browser, needs_close)

            if not using_tor:
                print("  Cannot retry without Tor — no circuit rotation available.")
                break

            if attempt < max_retries:
                _rotate_and_wait(config, attempt)
                continue
            else:
                print(f"  All {max_retries} attempts hit login wall. Giving up.")
                sys.exit(1)

        # Success — got a real page
        print(f"  Page loaded successfully on attempt {attempt}")
        return page, context, browser, needs_close

    print("ERROR: Could not load page (all attempts failed)")
    sys.exit(1)


def _rotate_and_wait(config, attempt):
    """Rotate Tor circuit and wait for it to establish."""
    print(f"  Rotating Tor circuit...")
    success = renew_tor_circuit(config)
    if success:
        print(f"  New circuit established. Retrying...")
    else:
        print(f"  Circuit rotation failed — retrying anyway...")
    # Increasing backoff: 3s, 5s, 8s, 12s...
    wait = 3 + attempt * 2
    time.sleep(wait)


def _close_session(context, browser, needs_close):
    """Clean up a browser session."""
    try:
        if needs_close:
            browser.close()
        else:
            context.close()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Test feed-page extraction")
    parser.add_argument("url", help="Facebook page URL to extract from")
    parser.add_argument("--save", action="store_true", help="Save to database")
    parser.add_argument("--headless", action="store_true", help="Headless browser")
    parser.add_argument("--images", action="store_true", help="Capture images via canvas")
    parser.add_argument("--json", action="store_true", help="Save JSON output")
    parser.add_argument("--no-tor", action="store_true", help="Skip Tor (direct connection)")
    parser.add_argument("--retries", type=int, default=5, help="Max Tor circuit retries (default: 5)")
    args = parser.parse_args()

    url = args.url.split("?")[0]
    if not url.startswith("https://"):
        url = "https://" + url.lstrip("http://")

    # Load config for Tor settings
    config = load_config()
    config["headless"] = args.headless

    tor_cfg = config.get("tor", {})
    using_tor = tor_cfg.get("enabled", False) and not args.no_tor

    if args.no_tor:
        config.setdefault("tor", {})["enabled"] = False

    print(f"\nFeed Extraction Test")
    print(f"  URL: {url}")
    print(f"  Headless: {args.headless}")
    print(f"  Tor: {using_tor}{'' if using_tor else ' (WARNING: direct connection)'}")
    print(f"  Max retries: {args.retries}")
    print(f"  Capture images: {args.images}")
    print(f"  Save to DB: {args.save}")
    print()

    if not using_tor and not args.no_tor:
        print("NOTE: Tor is not enabled in config.json. Set tor.enabled=true")
        print("      or use --no-tor to acknowledge direct connection.\n")

    with sync_playwright() as pw:
        # Navigate with auto-retry on login wall
        page, context, browser, needs_close = navigate_and_check(
            pw, config, url, max_retries=args.retries
        )

        # Scroll to load posts
        print("Scrolling to load posts...")
        human_scroll(page, scroll_count=5)
        page.wait_for_timeout(2000)

        # Inject the collector JS
        print("Injecting collector...")
        if not inject(page):
            print("ERROR: Failed to inject collector JS")
            _close_session(context, browser, needs_close)
            sys.exit(1)

        # Run the extraction pipeline
        print("\nRunning extraction pipeline...")
        posts = expand_and_extract(
            page,
            page_url=url,
            capture_images=args.images,
        )

        _close_session(context, browser, needs_close)

    # --- Print results ---
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {len(posts)} posts extracted")
    print(f"{'=' * 60}")

    for i, post in enumerate(posts, 1):
        comments = post.get("comments", [])
        images = post.get("image_urls", [])
        videos = post.get("video_urls", [])
        image_data = post.get("image_data", [])

        print(f"\n--- Post {i} ---")
        print(f"  ID:       {post.get('post_id', '?')}")
        print(f"  Author:   {post.get('author', '?')}")
        print(f"  URL:      {post.get('url', '?')}")
        print(f"  Time:     {post.get('timestamp', '?')}")

        text = post.get("text", "")
        if text:
            display_text = text[:200] + ("..." if len(text) > 200 else "")
            print(f"  Text:     {display_text}")
        else:
            print(f"  Text:     (empty)")

        if post.get("shared_from"):
            print(f"  Shared:   {post['shared_from']}")
        if post.get("reaction_count"):
            print(f"  Reacts:   {post['reaction_count']}")
        if images:
            print(f"  Images:   {len(images)} URLs")
        if image_data:
            print(f"  ImgData:  {len(image_data)} captured")
        if videos:
            print(f"  Videos:   {len(videos)} URLs")
        if post.get("links"):
            print(f"  Links:    {len(post['links'])}")

        print(f"  Comments: {len(comments)}")
        for j, c in enumerate(comments[:5], 1):
            author = c.get("author", "?")
            ctext = c.get("text", "")[:80]
            reply = " [reply]" if c.get("is_reply") else ""
            print(f"    {j}. {author}: {ctext}{reply}")
        if len(comments) > 5:
            print(f"    ... and {len(comments) - 5} more")

    # --- Save JSON ---
    if args.json:
        json_posts = []
        for p in posts:
            jp = {k: v for k, v in p.items() if k != "image_data"}
            json_posts.append(jp)

        out_path = "extract_output.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(json_posts, f, indent=2, ensure_ascii=False)
        print(f"\nJSON saved to {out_path}")

    # --- Save to database ---
    if args.save:
        from database import init_db, save_post, save_comments

        print("\nSaving to database...")
        init_db()

        saved = 0
        total_comments = 0
        for post in posts:
            comments = post.pop("comments", [])
            save_post(post, account="test_extract")
            if comments:
                save_comments(post["post_id"], comments)
                total_comments += len(comments)
            saved += 1

        print(f"  Saved {saved} posts, {total_comments} comments")

    # --- Summary ---
    total_comments = sum(len(p.get("comments", [])) for p in posts)
    total_images = sum(len(p.get("image_urls", [])) for p in posts)
    total_videos = sum(len(p.get("video_urls", [])) for p in posts)

    print(f"\nSummary:")
    print(f"  Posts:    {len(posts)}")
    print(f"  Comments: {total_comments}")
    print(f"  Images:   {total_images}")
    print(f"  Videos:   {total_videos}")


if __name__ == "__main__":
    main()
