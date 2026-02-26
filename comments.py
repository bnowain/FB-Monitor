"""
comments.py — Extract and incrementally update comments from Facebook posts.

Supports merging new comments into an existing comments.json file,
so the same post can be rechecked over a 24-hour window and accumulate
all comments without duplicates.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sanitize import is_garbage_comment

log = logging.getLogger("fb-monitor")


@dataclass
class Comment:
    author: str
    text: str
    timestamp: str = ""
    is_reply: bool = False
    strategy: str = ""
    depth: int = 0

    def to_dict(self) -> dict:
        return {
            "author": self.author,
            "text": self.text,
            "timestamp": self.timestamp,
            "is_reply": self.is_reply,
            "depth": self.depth,
        }

    @staticmethod
    def from_dict(d: dict) -> "Comment":
        return Comment(
            author=d.get("author", ""),
            text=d.get("text", ""),
            timestamp=d.get("timestamp", ""),
            is_reply=d.get("is_reply", False),
            depth=d.get("depth", 0),
        )


# ---------------------------------------------------------------------------
# Extraction strategies
# ---------------------------------------------------------------------------

def _strategy_aria(page) -> list[Comment]:
    """Extract comments via ARIA semantic containers."""
    comments = []
    try:
        results = page.eval_on_selector_all(
            'ul[role="list"] > li, div[aria-label*="comment" i], div[aria-label*="Comment" i]',
            """elements => elements.map(el => {
                const authorEl = el.querySelector('a[role="link"] > span > span')
                    || el.querySelector('a[role="link"] span')
                    || el.querySelector('a > strong')
                    || el.querySelector('a > b');
                const textEl = el.querySelector('div[dir="auto"]')
                    || el.querySelector('span[dir="auto"]');
                // Timestamp: prefer abbr/time, then short relative strings
                const abbrEl = el.querySelector('abbr[data-utime], abbr[title], time[datetime]');
                let timestamp = '';
                if (abbrEl) {
                    timestamp = abbrEl.getAttribute('title') || abbrEl.getAttribute('datetime') || abbrEl.textContent?.trim() || '';
                }
                if (!timestamp) {
                    el.querySelectorAll('a span, span').forEach(span => {
                        if (timestamp) return;
                        const t = span.textContent?.trim() || '';
                        if (/^\d+[hmdws]$/.test(t) || /^\d+\s*(hr|min|day|week)s?\s*(ago)?$/i.test(t)) {
                            timestamp = t;
                        }
                    });
                }
                // is_reply + depth: count nesting of li > ul > li chains
                let depth = 0;
                let cursor = el.closest('li');
                while (cursor) {
                    const ul = cursor.parentElement?.closest('ul[role="list"]');
                    const outerLi = ul?.closest('li');
                    if (outerLi && outerLi !== cursor) {
                        depth++;
                        cursor = outerLi;
                    } else {
                        break;
                    }
                }
                const isReply = depth > 0;
                return {
                    author: authorEl ? authorEl.innerText.trim() : '',
                    text: textEl ? textEl.innerText.trim() : '',
                    timestamp: timestamp,
                    isReply: isReply,
                    depth: depth
                };
            })"""
        )
        for item in results:
            text = item.get("text", "").strip()
            author = item.get("author", "Unknown")
            if text and len(text) >= 2 and not is_garbage_comment(author, text):
                depth = item.get("depth", 0)
                comments.append(Comment(
                    author=author,
                    text=text[:2000],
                    timestamp=item.get("timestamp", ""),
                    is_reply=item.get("isReply", False),
                    strategy="aria",
                    depth=depth,
                ))
    except Exception as e:
        log.debug(f"Comment strategy aria failed: {e}")
    return comments


def _strategy_mobile(page, browser_context, post_url: str) -> list[Comment]:
    """Extract comments from mobile site."""
    comments = []
    mobile_url = post_url.replace("www.facebook.com", "m.facebook.com")

    try:
        mp = browser_context.new_page()
        mp.goto(mobile_url, wait_until="domcontentloaded", timeout=30000)
        mp.wait_for_timeout(3000)

        # Expand comment threads
        for _ in range(8):
            try:
                more = mp.query_selector(
                    'a[href*="comment_id"]:has-text("View"), '
                    'div[id*="comment_replies_more"]'
                )
                if more:
                    more.click()
                    mp.wait_for_timeout(1500)
                else:
                    break
            except Exception:
                break

        results = mp.eval_on_selector_all(
            'div[data-sigil="comment-body"], div[id*="comment_body"], div.comment-body',
            """elements => elements.map(el => {
                const container = el.closest('[data-sigil="comment"]') || el.parentElement;
                const authorEl = container?.querySelector('a[data-sigil="actor-link"]')
                    || container?.querySelector('a > strong')
                    || container?.querySelector('h3 a');
                return {
                    author: authorEl ? authorEl.innerText.trim() : 'Unknown',
                    text: el.innerText.trim()
                };
            })"""
        )

        for item in results:
            text = item.get("text", "").strip()
            author = item.get("author", "Unknown")
            if text and len(text) >= 2 and not is_garbage_comment(author, text):
                comments.append(Comment(
                    author=author,
                    text=text[:2000],
                    strategy="mobile",
                ))
        mp.close()
    except Exception as e:
        log.debug(f"Comment strategy mobile failed: {e}")
    return comments


def _strategy_text_blocks(page) -> list[Comment]:
    """Extract comments by scanning text blocks."""
    comments = []
    try:
        results = page.eval_on_selector_all(
            'div[dir="auto"], span[dir="auto"]',
            """elements => {
                const seen = new Set();
                const noise = new Set([
                    'Like','Reply','Share','Comment','Write a comment',
                    'Write a comment…','Most relevant','All comments','Newest',
                    'Log In','Log in','Sign Up','Sign up','Create new account',
                    'Create New Account','Forgot Account?','Forgot account?',
                    'Forgot password?','Not now','See more','See More',
                    'No comments yet','No comments yet.','Be the first to comment.',
                    'Be the first to comment',
                    'Privacy','Privacy Policy','Terms','Terms of Service',
                    'Cookie Policy','Cookies','Ad Choices','About','Help',
                    'Contact','Careers','Meta','Meta Platforms, Inc.',
                    'English (US)','English (UK)','Español','Français',
                    'Deutsch','Português (Brasil)','Italiano',
                ]);
                const noisePatterns = [
                    /^Log in or sign up/i,
                    /^Sign up to see/i,
                    /^Create an account/i,
                    /^Join Facebook/i,
                    /^See more of/i,
                    /^All reactions/i,
                    /^\d+$/,
                    /^Most relevant/i,
                    /^Meta\s*[©(]/i,
                    /^See who reacted/i,
                    /^\d+\s*(comment|share)s?$/i,
                    /^\d+[hmdws]$/i,
                    /^\d+\s*(hr|min|sec|hour|minute|day|week)s?\s*(ago)?$/i,
                    /replied\s*$/i,
                    /^privacy\s*·\s*terms/i,
                ];
                return elements.filter(el => {
                    const text = el.innerText.trim();
                    if (text.length < 5 || text.length > 2000 || seen.has(text)) return false;
                    seen.add(text);
                    if (noise.has(text)) return false;
                    if (noisePatterns.some(p => p.test(text))) return false;
                    // Skip elements inside the main post content (not inside comment containers)
                    const inComment = el.closest('[aria-label*="comment" i]')
                        || el.closest('ul[role="list"]');
                    if (!inComment) return false;
                    return true;
                }).map(el => {
                    let parent = el.parentElement;
                    for (let i = 0; i < 5 && parent; i++) {
                        if (parent.getAttribute('role') === 'article') break;
                        parent = parent.parentElement;
                    }
                    const authorEl = parent?.querySelector('a[role="link"] span span')
                        || parent?.querySelector('a > strong');
                    return {
                        author: authorEl ? authorEl.innerText.trim() : '',
                        text: el.innerText.trim()
                    };
                });
            }"""
        )
        for item in results:
            text = item.get("text", "")
            author = item.get("author", "")
            if len(text) >= 10 and not is_garbage_comment(author, text):
                comments.append(Comment(
                    author=author,
                    text=text[:2000],
                    strategy="text_blocks",
                ))
    except Exception as e:
        log.debug(f"Comment strategy text_blocks failed: {e}")
    return comments


# ---------------------------------------------------------------------------
# Expand all comment threads
# ---------------------------------------------------------------------------

def _expand_comments(page):
    """Click through 'View more comments' and 'See more replies'."""
    for _ in range(15):
        clicked = False
        for selector in [
            'div[role="button"]:has-text("View more comments")',
            'div[role="button"]:has-text("See more")',
            'div[role="button"]:has-text("more comments")',
            'div[role="button"]:has-text("more replies")',
            'div[role="button"]:has-text("View")',
        ]:
            try:
                buttons = page.query_selector_all(selector)
                for btn in buttons:
                    if btn.is_visible():
                        btn.click()
                        clicked = True
                        page.wait_for_timeout(1500)
            except Exception:
                continue
        if not clicked:
            break

    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Deduplication / merging
# ---------------------------------------------------------------------------

def _comment_key(c: Comment) -> str:
    """Generate a stable key for deduplication."""
    # Use author + normalized text prefix as the key
    text_norm = re.sub(r'\s+', ' ', c.text.strip().lower())[:150]
    return f"{c.author.lower().strip()}|{text_norm}"


def _deduplicate(comments: list[Comment]) -> list[Comment]:
    seen = {}
    for c in comments:
        key = _comment_key(c)
        if key not in seen:
            seen[key] = c
        else:
            # Prefer the one with more metadata
            existing = seen[key]
            if (c.timestamp and not existing.timestamp) or (c.author and not existing.author):
                seen[key] = c
    return list(seen.values())


def merge_comments(existing: list[Comment], new: list[Comment]) -> tuple[list[Comment], int]:
    """
    Merge new comments into existing list without duplicates.
    Returns (merged_list, count_of_new_comments_added).
    """
    existing_keys = {_comment_key(c) for c in existing}
    added = 0

    for c in new:
        key = _comment_key(c)
        if key not in existing_keys:
            existing.append(c)
            existing_keys.add(key)
            added += 1

    return existing, added


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_comments_file(path: Path) -> list[Comment]:
    """Load comments from a JSON file."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [Comment.from_dict(d) for d in data.get("comments", [])]
    except Exception as e:
        log.warning(f"Failed to load comments file {path}: {e}")
        return []


def save_comments_file(path: Path, comments: list[Comment], post_url: str = ""):
    """Save comments to a JSON file with metadata."""
    data = {
        "post_url": post_url,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_comments": len(comments),
        "comments": [c.to_dict() for c in comments],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------

def extract_comments(page, browser_context=None, post_url: str = "") -> list[Comment]:
    """
    Extract comments from a Facebook post page.
    The page should already be navigated to the post.
    """
    _expand_comments(page)

    strategies = [
        ("aria", lambda: _strategy_aria(page)),
        ("text_blocks", lambda: _strategy_text_blocks(page)),
    ]

    if browser_context and post_url:
        strategies.insert(1, (
            "mobile",
            lambda: _strategy_mobile(page, browser_context, post_url),
        ))

    best = []
    best_name = ""

    for name, fn in strategies:
        try:
            results = fn()
            if results:
                log.info(f"  Comment strategy '{name}' found {len(results)}")
                if len(results) > len(best):
                    best = results
                    best_name = name
        except Exception as e:
            log.warning(f"  Comment strategy '{name}' failed: {e}")

    if best:
        log.info(f"  Using {len(best)} comments from '{best_name}'")

    return _deduplicate(best)
