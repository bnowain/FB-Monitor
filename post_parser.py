"""
post_parser.py — Extract structured data from a Facebook post page.

Navigates to an individual post and extracts:
- Post text / body
- Date and time posted
- Whether it was shared from another page/person
- Any links in the post
- Attachment URLs (images, videos) for downloading
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("fb-monitor")


@dataclass
class PostData:
    post_id: str = ""
    url: str = ""
    page_name: str = ""
    author: str = ""
    text: str = ""
    timestamp: str = ""
    timestamp_raw: str = ""
    shared_from: str = ""          # original source if shared
    shared_original_url: str = ""
    links: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    reaction_count: str = ""
    comment_count_text: str = ""
    share_count_text: str = ""
    extraction_strategy: str = ""

    def to_dict(self) -> dict:
        return {
            "post_id": self.post_id,
            "url": self.url,
            "page_name": self.page_name,
            "author": self.author,
            "text": self.text,
            "timestamp": self.timestamp,
            "timestamp_raw": self.timestamp_raw,
            "shared_from": self.shared_from,
            "shared_original_url": self.shared_original_url,
            "links": self.links,
            "image_urls": self.image_urls,
            "video_urls": self.video_urls,
            "reaction_count": self.reaction_count,
            "comment_count_text": self.comment_count_text,
            "share_count_text": self.share_count_text,
        }


# ---------------------------------------------------------------------------
# Strategy 1: Structured DOM parsing (desktop)
# ---------------------------------------------------------------------------

def _parse_structured(page) -> PostData:
    """Parse post data from Facebook's desktop DOM."""
    data = PostData(extraction_strategy="structured_dom")

    try:
        result = page.evaluate("""() => {
            const out = {
                author: '',
                text: '',
                timestamp: '',
                timestampRaw: '',
                sharedFrom: '',
                sharedOriginalUrl: '',
                links: [],
                imageUrls: [],
                videoUrls: [],
                reactionCount: '',
                commentCount: '',
                shareCount: ''
            };

            // --- Author ---
            // The post author is usually the first prominent link with a name
            const authorEl = document.querySelector('h2 a, h3 a, [data-ad-rendering-role="profile_name"] a, a[role="link"] > strong');
            if (authorEl) {
                out.author = authorEl.innerText.trim();
            }

            // --- Post text ---
            // Post body is typically in dir="auto" divs within the main content
            const textBlocks = document.querySelectorAll('[data-ad-rendering-role="story_message"] div[dir="auto"], div[data-ad-preview="message"] div[dir="auto"]');
            if (textBlocks.length > 0) {
                out.text = Array.from(textBlocks).map(el => el.innerText.trim()).join('\\n');
            } else {
                // Broader fallback: find the largest text block in an article
                const articles = document.querySelectorAll('[role="article"]');
                if (articles.length > 0) {
                    const article = articles[0];
                    const allText = article.querySelectorAll('div[dir="auto"]');
                    let longest = '';
                    allText.forEach(el => {
                        const t = el.innerText.trim();
                        if (t.length > longest.length && t.length > 20) {
                            longest = t;
                        }
                    });
                    out.text = longest;
                }
            }

            // --- Timestamp ---
            // Timestamps are usually in <a> tags with aria-label containing a date
            const timeLinks = document.querySelectorAll('a[href*="/posts/"], a[href*="/permalink/"], a[href*="story_fbid"], a[href*="/pfbid"]');
            for (const link of timeLinks) {
                const label = link.getAttribute('aria-label') || '';
                const text = link.innerText.trim();
                // Facebook timestamps: "January 15, 2025 at 3:45 PM" or "2h" or "Yesterday at 5:00 PM"
                if (label && /\\d{4}|\\d+[hms]|yesterday|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday/i.test(label)) {
                    out.timestamp = label;
                    out.timestampRaw = text;
                    break;
                }
                if (/^(\\d+[hms]|\\d+\\s*(hr|min|sec|hour|minute|day)s?( ago)?|Just now|Yesterday|Today|\\d+d)$/i.test(text)) {
                    out.timestampRaw = text;
                    if (!out.timestamp && label) out.timestamp = label;
                    break;
                }
            }

            // Also check <abbr> and <time> elements
            if (!out.timestamp) {
                const abbr = document.querySelector('abbr[data-utime], abbr[title], time[datetime]');
                if (abbr) {
                    out.timestamp = abbr.getAttribute('title') || abbr.getAttribute('datetime') || abbr.innerText;
                    out.timestampRaw = abbr.innerText;
                }
            }

            // --- Shared from ---
            // Shared posts typically have a secondary author/page link
            const shareIndicators = document.querySelectorAll('span');
            for (const span of shareIndicators) {
                const t = span.innerText.trim().toLowerCase();
                if (t.includes('shared') && (t.includes('post') || t.includes('photo') || t.includes('video') || t.includes('link'))) {
                    // The shared-from source is usually a link near this text
                    const container = span.closest('div');
                    if (container) {
                        const shareLink = container.querySelector('a[role="link"]');
                        if (shareLink && shareLink.innerText.trim() !== out.author) {
                            out.sharedFrom = shareLink.innerText.trim();
                            out.sharedOriginalUrl = shareLink.href || '';
                        }
                    }
                    break;
                }
            }

            // --- Links in post ---
            // External links shared in the post
            const allLinks = document.querySelectorAll('a[href]');
            const externalLinks = new Set();
            for (const a of allLinks) {
                const href = a.href;
                if (href && !href.includes('facebook.com') && !href.includes('fbcdn') &&
                    href.startsWith('http') && !href.includes('l.facebook.com/l.php')) {
                    externalLinks.add(href);
                }
                // Facebook's link shim
                if (href && href.includes('l.facebook.com/l.php')) {
                    try {
                        const url = new URL(href);
                        const actual = url.searchParams.get('u');
                        if (actual) externalLinks.add(actual);
                    } catch(e) {}
                }
            }
            out.links = Array.from(externalLinks);

            // --- Images ---
            const images = document.querySelectorAll('img[src*="fbcdn"], img[src*="scontent"]');
            const imgUrls = new Set();
            for (const img of images) {
                const src = img.src;
                // Filter out tiny icons/avatars (profile pics are usually small)
                const width = img.naturalWidth || img.width || 0;
                if (width > 200 || src.includes('/p') || src.includes('_n.')) {
                    imgUrls.add(src);
                }
            }
            out.imageUrls = Array.from(imgUrls);

            // --- Videos ---
            const videos = document.querySelectorAll('video[src], video source[src]');
            const vidUrls = new Set();
            for (const v of videos) {
                const src = v.src || v.getAttribute('src');
                if (src) vidUrls.add(src);
            }
            out.videoUrls = Array.from(vidUrls);

            // --- Engagement counts ---
            // Reaction count
            const reactionEl = document.querySelector('[aria-label*="reaction"], [aria-label*="like"], span[role="toolbar"]');
            if (reactionEl) {
                const label = reactionEl.getAttribute('aria-label') || '';
                if (label) out.reactionCount = label;
            }

            return out;
        }""")

        data.author = result.get("author", "")
        data.text = result.get("text", "")
        data.timestamp = result.get("timestamp", "")
        data.timestamp_raw = result.get("timestampRaw", "")
        data.shared_from = result.get("sharedFrom", "")
        data.shared_original_url = result.get("sharedOriginalUrl", "")
        data.links = result.get("links", [])
        data.image_urls = result.get("imageUrls", [])
        data.video_urls = result.get("videoUrls", [])
        data.reaction_count = result.get("reactionCount", "")
        data.comment_count_text = result.get("commentCount", "")
        data.share_count_text = result.get("shareCount", "")

    except Exception as e:
        log.warning(f"  Structured DOM parsing failed: {e}")

    return data


# ---------------------------------------------------------------------------
# Strategy 2: Mobile site parsing
# ---------------------------------------------------------------------------

def _parse_mobile(page, browser_context, post_url: str) -> PostData:
    """Parse post data from Facebook's mobile DOM."""
    data = PostData(extraction_strategy="mobile_dom")

    mobile_url = post_url.replace("www.facebook.com", "m.facebook.com")

    try:
        mp = browser_context.new_page()
        mp.goto(mobile_url, wait_until="domcontentloaded", timeout=30000)
        mp.wait_for_timeout(3000)

        result = mp.evaluate("""() => {
            const out = { author: '', text: '', timestamp: '', sharedFrom: '', links: [], imageUrls: [], videoUrls: [] };

            // Author
            const header = document.querySelector('h3 a, strong a, [data-sigil="actor-link"]');
            if (header) out.author = header.innerText.trim();

            // Post text - mobile uses simpler structure
            const storyBody = document.querySelector('[data-sigil="m-story-body-text"], div.story_body_container > div');
            if (storyBody) {
                out.text = storyBody.innerText.trim();
            } else {
                // Fallback
                const paras = document.querySelectorAll('p');
                out.text = Array.from(paras).map(p => p.innerText.trim()).filter(t => t.length > 10).join('\\n');
            }

            // Timestamp
            const timeEl = document.querySelector('abbr, [data-sigil="timestamp"]');
            if (timeEl) {
                out.timestamp = timeEl.getAttribute('title') || timeEl.getAttribute('data-store') || timeEl.innerText.trim();
            }

            // Shared from
            const shareHeader = document.querySelector('[data-sigil="share-header"]');
            if (shareHeader) {
                const link = shareHeader.querySelector('a');
                out.sharedFrom = link ? link.innerText.trim() : shareHeader.innerText.trim();
            }

            // Images
            const imgs = document.querySelectorAll('img[src*="fbcdn"], img[src*="scontent"]');
            imgs.forEach(img => {
                if ((img.width || 0) > 100) out.imageUrls.push(img.src);
            });

            // Video source URLs
            const vids = document.querySelectorAll('video[src], video source[src]');
            vids.forEach(v => { if (v.src) out.videoUrls.push(v.src); });

            // Links
            const allLinks = document.querySelectorAll('a[href]');
            const extLinks = new Set();
            allLinks.forEach(a => {
                const href = a.href;
                if (href && !href.includes('facebook.com') && href.startsWith('http')) {
                    extLinks.add(href);
                }
            });
            out.links = Array.from(extLinks);

            return out;
        }""")

        data.author = result.get("author", "")
        data.text = result.get("text", "")
        data.timestamp = result.get("timestamp", "")
        data.shared_from = result.get("sharedFrom", "")
        data.links = result.get("links", [])
        data.image_urls = result.get("imageUrls", [])
        data.video_urls = result.get("videoUrls", [])

        mp.close()

    except Exception as e:
        log.warning(f"  Mobile DOM parsing failed: {e}")

    return data


# ---------------------------------------------------------------------------
# Strategy 3: Raw text extraction (fallback)
# ---------------------------------------------------------------------------

def _parse_raw_text(page) -> PostData:
    """Just grab as much text as possible from the post page."""
    data = PostData(extraction_strategy="raw_text")

    try:
        data.text = page.evaluate("""() => {
            const article = document.querySelector('[role="article"]');
            if (article) return article.innerText.substring(0, 5000);
            return document.body.innerText.substring(0, 5000);
        }""")
    except Exception as e:
        log.warning(f"  Raw text extraction failed: {e}")

    return data


# ---------------------------------------------------------------------------
# Merge helper
# ---------------------------------------------------------------------------

def _merge_post_data(primary: PostData, fallback: PostData) -> PostData:
    """Fill in missing fields from the fallback."""
    if not primary.text and fallback.text:
        primary.text = fallback.text
    if not primary.author and fallback.author:
        primary.author = fallback.author
    if not primary.timestamp and fallback.timestamp:
        primary.timestamp = fallback.timestamp
    if not primary.shared_from and fallback.shared_from:
        primary.shared_from = fallback.shared_from
    if not primary.shared_original_url and fallback.shared_original_url:
        primary.shared_original_url = fallback.shared_original_url
    if not primary.links and fallback.links:
        primary.links = fallback.links
    if not primary.image_urls and fallback.image_urls:
        primary.image_urls = fallback.image_urls
    if not primary.video_urls and fallback.video_urls:
        primary.video_urls = fallback.video_urls
    return primary


# ---------------------------------------------------------------------------
# Main parser pipeline
# ---------------------------------------------------------------------------

def parse_post(page, browser_context=None, post_url: str = "", post_id: str = "", page_name: str = "") -> PostData:
    """
    Extract structured data from a Facebook post page.

    The page should already be navigated to the post URL.
    Runs multiple strategies and merges results.
    """

    # Strategy 1: Structured DOM
    data = _parse_structured(page)
    data.post_id = post_id
    data.url = post_url
    data.page_name = page_name

    # If we got good text, we're probably fine
    has_text = bool(data.text and len(data.text) > 10)

    # Strategy 2: Mobile fallback for missing fields
    if browser_context and post_url and not has_text:
        mobile_data = _parse_mobile(page, browser_context, post_url)
        data = _merge_post_data(data, mobile_data)

    # Strategy 3: Raw text as last resort
    if not data.text or len(data.text) < 10:
        raw_data = _parse_raw_text(page)
        data = _merge_post_data(data, raw_data)

    # Clean up
    data.text = data.text.strip()
    data.links = list(set(data.links))  # dedupe
    data.image_urls = list(set(data.image_urls))
    data.video_urls = list(set(data.video_urls))

    log.info(f"  Parsed post: {len(data.text)} chars, {len(data.image_urls)} images, "
             f"{len(data.video_urls)} videos, shared_from='{data.shared_from}'")

    return data
