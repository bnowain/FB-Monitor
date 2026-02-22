"""
extractors.py — Resilient post extraction with multiple strategies.

Facebook changes its DOM frequently. This module uses a chain of extraction
strategies ordered from most reliable to least. If the primary strategy stops
finding posts, it automatically falls back to the next one.

A health check system tracks which strategies are working and alerts you
when all strategies degrade.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("fb-monitor")

HEALTH_FILE = Path(__file__).parent / "extractor_health.json"


@dataclass
class ExtractedPost:
    url: str
    id: str
    text: str = ""
    strategy: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_url(href: str) -> str:
    href = re.split(r'[?&]__cft__', href)[0]
    href = re.split(r'[?&]__tn__', href)[0]
    return href


def _extract_post_id(url: str) -> Optional[str]:
    patterns = [
        (r'/posts/([\w]+)', 1),
        (r'/(pfbid[\w]+)', 1),
        (r'story_fbid=(\d+)', 1),
        (r'/videos/(\d+)', 1),
        (r'/reel/(\d+)', 1),
        (r'/permalink/(\d+)', 1),
        (r'fbid=(\d+)', 1),
    ]
    for pattern, group in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(group)
    return None


POST_URL_PATTERNS = [
    r'/posts/', r'/permalink/', r'/videos/', r'/watch/',
    r'/reel/', r'/photo', r'story_fbid=', r'/pfbid',
]


def _is_post_url(href: str) -> bool:
    return any(pat in href for pat in POST_URL_PATTERNS)


# ---------------------------------------------------------------------------
# Strategy 1: ARIA role="article" containers
# ---------------------------------------------------------------------------

def strategy_aria_articles(page) -> list[ExtractedPost]:
    posts = []
    seen = set()
    try:
        results = page.eval_on_selector_all(
            '[role="article"]',
            """articles => articles.map(article => ({
                links: Array.from(article.querySelectorAll('a[href]')).map(a => a.href),
                text: article.innerText.substring(0, 300)
            }))"""
        )
        for article in results:
            for href in article.get("links", []):
                href = _normalize_url(href)
                if _is_post_url(href) and href not in seen:
                    post_id = _extract_post_id(href)
                    if post_id:
                        seen.add(href)
                        posts.append(ExtractedPost(
                            url=href, id=post_id,
                            text=article.get("text", "")[:200],
                            strategy="aria_articles",
                        ))
                        break
    except Exception as e:
        log.debug(f"Strategy aria_articles failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Strategy 2: Timestamp anchor links
# ---------------------------------------------------------------------------

def strategy_timestamp_anchors(page) -> list[ExtractedPost]:
    posts = []
    seen = set()
    try:
        results = page.eval_on_selector_all(
            "a[href]",
            """elements => elements.filter(el => {
                const text = el.innerText.trim();
                return /^(\\d+[hms]|\\d+\\s*(hr|min|sec|hour|minute)s?( ago)?|Just now|Yesterday|\\d+d)$/i.test(text);
            }).map(el => {
                let container = el.closest('[role="article"]') || el.parentElement?.parentElement?.parentElement;
                return {
                    href: el.href,
                    text: container ? container.innerText.substring(0, 300) : ''
                };
            })"""
        )
        for item in results:
            href = _normalize_url(item.get("href", ""))
            if _is_post_url(href) and href not in seen:
                post_id = _extract_post_id(href)
                if post_id and post_id not in seen:
                    seen.add(post_id)
                    seen.add(href)
                    posts.append(ExtractedPost(
                        url=href, id=post_id,
                        text=item.get("text", "")[:200],
                        strategy="timestamp_anchors",
                    ))
    except Exception as e:
        log.debug(f"Strategy timestamp_anchors failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Strategy 3: Link sweep (all <a> tags)
# ---------------------------------------------------------------------------

def strategy_link_sweep(page) -> list[ExtractedPost]:
    posts = []
    seen = set()
    try:
        links = page.eval_on_selector_all(
            "a[href]",
            """elements => elements.map(el => ({
                href: el.href,
                text: el.innerText.substring(0, 200)
            }))"""
        )
        for link in links:
            href = _normalize_url(link.get("href", ""))
            if _is_post_url(href) and href not in seen:
                post_id = _extract_post_id(href)
                if post_id and post_id not in seen:
                    seen.add(post_id)
                    seen.add(href)
                    posts.append(ExtractedPost(
                        url=href, id=post_id,
                        text=link.get("text", "").strip()[:200],
                        strategy="link_sweep",
                    ))
    except Exception as e:
        log.debug(f"Strategy link_sweep failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Strategy 4: Mobile site
# ---------------------------------------------------------------------------

def strategy_mobile_page(page, browser_context, page_url: str) -> list[ExtractedPost]:
    posts = []
    seen = set()
    mobile_url = page_url.replace("www.facebook.com", "m.facebook.com")
    if mobile_url == page_url:
        return []
    try:
        mp = browser_context.new_page()
        mp.goto(mobile_url, wait_until="domcontentloaded", timeout=30000)
        mp.wait_for_timeout(3000)
        links = mp.eval_on_selector_all(
            "a[href]",
            """elements => elements.map(el => ({
                href: el.href,
                text: el.innerText.substring(0, 200)
            }))"""
        )
        for link in links:
            href = _normalize_url(link.get("href", ""))
            if _is_post_url(href) and href not in seen:
                post_id = _extract_post_id(href)
                if post_id and post_id not in seen:
                    seen.add(post_id)
                    seen.add(href)
                    posts.append(ExtractedPost(
                        url=href, id=post_id,
                        text=link.get("text", "").strip()[:200],
                        strategy="mobile_page",
                    ))
        mp.close()
    except Exception as e:
        log.debug(f"Strategy mobile_page failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Strategy 5: Raw HTML regex (nuclear option)
# ---------------------------------------------------------------------------

def strategy_raw_html(page) -> list[ExtractedPost]:
    posts = []
    seen = set()
    try:
        html = page.content()
        pattern = r'https?://(?:www\.|m\.)?facebook\.com/[^"\'<>\s]+?(?:/posts/|/permalink/|/videos/|/reel/|/pfbid|story_fbid=)[^"\'<>\s]*'
        matches = re.findall(pattern, html)
        for href in matches:
            href = _normalize_url(href)
            href = href.replace("\\u0025", "%").replace("\\/", "/")
            href = re.sub(r'\\u[\da-fA-F]{4}', '', href)
            post_id = _extract_post_id(href)
            if post_id and post_id not in seen:
                seen.add(post_id)
                posts.append(ExtractedPost(
                    url=href, id=post_id, text="", strategy="raw_html",
                ))
    except Exception as e:
        log.debug(f"Strategy raw_html failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Health tracking
# ---------------------------------------------------------------------------

def _load_health() -> dict:
    if HEALTH_FILE.exists():
        with open(HEALTH_FILE, "r") as f:
            return json.load(f)
    return {}


def _save_health(health: dict):
    with open(HEALTH_FILE, "w") as f:
        json.dump(health, f, indent=2)


def update_health(strategy_name: str, found_count: int):
    health = _load_health()
    if strategy_name not in health:
        health[strategy_name] = {
            "total_runs": 0, "total_found": 0,
            "consecutive_zeros": 0, "last_success": None, "last_run": None,
        }
    entry = health[strategy_name]
    entry["total_runs"] += 1
    entry["total_found"] += found_count
    entry["last_run"] = datetime.now().isoformat()
    if found_count > 0:
        entry["consecutive_zeros"] = 0
        entry["last_success"] = datetime.now().isoformat()
    else:
        entry["consecutive_zeros"] += 1
    _save_health(health)

    if entry["consecutive_zeros"] >= 5:
        log.warning(f"⚠️  Strategy '{strategy_name}' has found 0 posts for "
                    f"{entry['consecutive_zeros']} consecutive runs.")


def get_health_report() -> str:
    health = _load_health()
    if not health:
        return "No extraction data yet."
    lines = ["Extractor Health Report", "=" * 40]
    for name, data in health.items():
        avg = data["total_found"] / max(data["total_runs"], 1)
        status = "✅" if data["consecutive_zeros"] < 5 else "⚠️"
        lines.append(f"\n{status} {name}")
        lines.append(f"   Runs: {data['total_runs']} | Avg posts: {avg:.1f}")
        lines.append(f"   Consecutive zeros: {data['consecutive_zeros']}")
        lines.append(f"   Last success: {data.get('last_success', 'Never')}")
    all_degraded = all(d["consecutive_zeros"] >= 5 for d in health.values() if d["total_runs"] > 0)
    if all_degraded:
        lines.append("\n🚨 ALL STRATEGIES DEGRADED — Facebook likely changed their DOM.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------

def extract_posts(page, browser_context=None, page_url: str = "") -> list[ExtractedPost]:
    all_posts: dict[str, ExtractedPost] = {}

    strategies = [
        ("aria_articles", lambda: strategy_aria_articles(page)),
        ("timestamp_anchors", lambda: strategy_timestamp_anchors(page)),
        ("link_sweep", lambda: strategy_link_sweep(page)),
        ("raw_html", lambda: strategy_raw_html(page)),
    ]

    if browser_context and page_url:
        strategies.insert(3, (
            "mobile_page",
            lambda: strategy_mobile_page(page, browser_context, page_url),
        ))

    for name, fn in strategies:
        try:
            results = fn()
            update_health(name, len(results))
            for post in results:
                if post.id not in all_posts:
                    all_posts[post.id] = post
            if results:
                log.info(f"  Strategy '{name}' found {len(results)} post(s)")
        except Exception as e:
            log.warning(f"  Strategy '{name}' error: {e}")
            update_health(name, 0)

    if not all_posts:
        log.warning("  ⚠️  No strategies found any posts.")

    return list(all_posts.values())
