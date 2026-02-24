# Social Media Archival Pipeline — Research & Proposal

**Date:** 2026-02-24
**Context:** Extending FB-Monitor into a multi-platform public official social media archive
**Goal:** Journalistic accountability + archival (posts, comments, media links) — not marketing

---

## PART 1 — Best DIY Approach (Executive Summary)

### What FB-Monitor Already Does Well

FB-Monitor is a surprisingly mature Facebook-specific archival system. After a thorough codebase review, here's what's already built and working:

**Strengths of the current system:**

1. **Multi-strategy extraction with automatic fallback** (`extractors.py`): 5 independent DOM-parsing strategies (ARIA articles, data-pagelet containers, feed unit divs, generic link scanning, and a JS-injected collector). When Facebook changes their DOM, the system degrades gracefully instead of going fully blind. Health tracking (`extractor_health.json`) monitors which strategies are working.

2. **Sophisticated anti-detection** (`stealth.py`): Coherent browser fingerprints (UA + platform + vendor matched), WebGL renderer spoofing, viewport rotation, human-like scroll patterns with jitter, and per-account rate limiting. The Tor pool with instance racing (`_probe_tor_instances`) is genuinely clever — racing multiple Tor exit nodes in parallel and using the first one that doesn't hit a login wall.

3. **Two-tier account system** (`sessions.py`): Anonymous/Tor sessions for aggressive polling (30 req/hr) and persistent logged-in profiles for conservative access (8 req/hr). Pages are mapped to accounts, and each gets appropriate timing.

4. **3-phase scraping cycle** (`fb_monitor.py`):
   - Phase 1: Detect new posts (feed scanning)
   - Phase 2: Recheck comments on tracked posts (24-hour window, 30-min intervals)
   - Phase 3: Process import queue (manual URL backfill)

5. **JS-injected collector** (`collector.py` + `injected_collector.js`): A 5-phase in-page pipeline that opens comment sections, switches to "All comments", expands threads, extracts posts+comments, and captures images via canvas. This runs entirely in the page context.

6. **Media pipeline** (`downloader.py`): 3 download modes (direct CDN, SOCKS5/Tor proxy, remote VPS proxy). Uses yt-dlp for video. Queues logged-in media for manual review to avoid fingerprinting.

7. **Comprehensive web UI + REST API** (`web_ui.py`): FastAPI serving HTML UI for browsing posts/comments/people/entities, plus a full REST API for Atlas integration. Includes the `/api/ingest` endpoint for browser extension data.

8. **Rich relational model** (`database.py`): 14 tables covering posts, comments, attachments, people (linked to pages/posts/comments), entities (organizations), categories, import queue, and media queue. Good indexing.

### What's Limited or Missing

| Gap | Impact | Difficulty to fix |
|-----|--------|-------------------|
| **No comment threading** — `is_reply` boolean only, no `parent_id` or `depth` | Can't reconstruct conversation threads | Medium |
| **No edit/delete detection** — no content hashing, no version history | Can't track when posts/comments are modified or removed | Medium |
| **No `first_seen`/`last_seen` audit trail** on comments | Can't prove when content appeared or disappeared | Easy |
| **Dedup is URL+text-based** — comments use `UNIQUE(post_id, author, text)` | Misses edits, can't track same comment across text changes | Medium |
| **No platform-native IDs for comments** — no `comment_id` column | Can't deduplicate reliably if author changes display name | Medium |
| **State split between JSON and SQLite** — `state.json` for tracking jobs, SQLite for data | Race conditions possible, state can diverge | Medium |
| **Single-platform** — hardcoded for Facebook DOM parsing | Can't extend to other platforms without rewriting | High (architectural) |
| **No content hashing** — no SHA-256 of post text or media list | Can't efficiently detect edits | Easy |
| **No scheduled lookback strategy** — only 24hr comment tracking window | Misses late comments, no 7d/30d sweeps | Easy |
| **Fragile DOM selectors** — every Facebook redesign can break extraction | Need constant maintenance | Inherent to approach |
| **No RSS/API fallback** — purely Playwright-based | When browser extraction fails, there's no backup data source | Medium |

### The Cheapest Compliant Path

**Build yourself:**
- Multi-platform collector framework (extending FB-Monitor's architecture)
- Normalizer to map all sources into one common schema
- Storage layer (upgrade existing SQLite or move to PostgreSQL)
- Scheduler with tiered polling cadence
- Edit/delete detection via content hashing

**Use official channels where available:**
- YouTube Data API v3 (free, 10K units/day — best bang for zero bucks)
- Bluesky AT Protocol (free, open, no auth needed for public data, IETF standardization underway)
- Mastodon API (free, open, 300 req/5min — generous)
- Meta Content Library API ($371/mo via SOMAR for qualifying academic/nonprofit researchers — civil society orgs and fact-checkers eligible; for-profit news outlets excluded)
- X/Twitter API Basic tier ($100-200/mo) or Pro ($5K/mo) — academic research tier was sunsetted in 2023

**What you should NOT build yourself:**
- A CrowdTangle replacement (it's dead, Meta Content Library replaces it)
- Instagram scraper (detection is aggressive, use official API)
- A Tor anonymization layer for platforms other than Facebook (overkill, and other platforms have better official access)

---

## PART 2 — Feasibility Matrix (by Platform)

| Platform | Compliant Access Method | Posts? | Comments? | Media URLs? | Cost | Notes/Risks |
|----------|------------------------|--------|-----------|-------------|------|-------------|
| **Facebook Pages** | Meta Content Library API (researchers); Graph API v22 (Page owners only); Playwright scraping (current approach) | Yes (all methods) | MCL: Yes (searchable since 2025, 100+ data fields). Graph: Yes (if Page owner). Scraping: Yes (DOM-parsed) | MCL: No direct URLs. Graph: Yes. Scraping: Yes (CDN URLs, ephemeral) | MCL via SOMAR: **$371/mo per team + $1K one-time fee** (free compute ended Dec 2025). Graph: Free. Scraping: Free + Tor costs | MCL requires academic or nonprofit affiliation (universities, research institutes, civil society orgs, fact-checkers — NOT for-profit news outlets). Apply through SOMAR at ICPSR. Graph API cannot read arbitrary third-party page posts. CrowdTangle shut down Aug 2024. MCL limit: 500K records/7 days. |
| **Instagram** | Instagram Graph API (business/creator accounts); Meta Content Library (researchers) | Graph: Yes (own accounts). MCL: Yes (public) | Graph: Yes (own posts only, read/reply/delete). MCL: Limited | Graph: Yes. MCL: No | Free | Basic Display API **retired Dec 4, 2024**. Graph API requires Business/Creator account linked to FB Page. No public discovery of arbitrary users' content. Several metrics deprecated in v21+ (Jan 2025). MCL is the only path for third-party data. |
| **X / Twitter** | X API v2 (Free, Basic, Pro, Enterprise tiers) | Free: ~0 reads. Basic: 10K/mo. Pro: 1M/mo | Basic: No search. Pro: Full archive search + replies | Yes (media URLs in tweet expansions) | Free: $0 (write-only). Basic: **$100-200/mo**. Pro: **$5,000/mo**. Enterprise: **$42,000+/mo** | Free tier is useless for reading. Basic is marginal. Pro is the realistic minimum for archival. **Academic Research tier sunsetted 2023** — no free research access exists. Pay-per-use in closed beta ($500 voucher for testers, Dec 2025). Enterprise costs up ~9,900% since 2022. |
| **YouTube** | YouTube Data API v3 | Yes (channels, playlists, search) | Yes (comment threads with `parentId` threading!) | Yes (thumbnail URLs; video streams need yt-dlp) | **Free** (10K quota units/day) | Best official API for this use case. Each list request = 1 unit, search = 100 units. ~100 channel checks + ~500 comment fetches/day easily fits quota. YouTube Researcher Program may grant quota increases. No video download or transcript access via API. |
| **TikTok** | TikTok Research API | Yes (metadata + `voice_to_text` for ~20% of videos) | **Yes** (100 per request, full retrieval) | No (video download not available) | Free (academic only) | Requires **non-profit university** affiliation (US/EU only). 1K requests/day, 100K records/day. 30-day query windows. Data must refresh every 15 days. Application can take 4+ weeks (one case: 21 months). AI Forensics found 1 in 8 videos unretrievable. No journalist/civic program. |
| **Threads** | Threads API (launched June 2024, expanding rapidly) | Own posts + **public profile discovery** (July 2025) + **keyword search** (2025) | Own account replies only | Own account only | Free | Keyword search: 500 queries/7-day window (~71/day). Sensitive keywords return empty results. Public profiles retrievable since July 2025. MCL includes Threads posts from accounts with 1,000+ followers (since Feb 2025). API still young — expect breaking changes. |
| **Bluesky** | AT Protocol (public, open, **IETF standardization underway**) | Yes (full firehose + per-account + full-text search with date ranges) | Yes (full thread trees via `getPostThread`) | Yes (blob CDN URLs) | **Free, no auth needed** for public reads | Best-case scenario. 3,000 req/5min per IP. Firehose: real-time WebSocket of ALL public activity (no auth). Jetstream: simplified JSON alternative. ~196M users. Running your own PDS gives unlimited access. |
| **Mastodon** | Mastodon REST API + Streaming API | Yes (per-instance public timeline + per-account) | Yes (thread context: unauth 40 ancestors/60 descendants; auth 4,096/4,096 unlimited depth) | Yes (attachment URLs in status objects) | **Free** (open source) | 300 req/5min per user, 7,500/5min per IP. Federated = must query each instance separately. Mastodon 4.5 (Oct 2025) adds async reply fetching. Instance operators can disable public API. Instances can shut down permanently. |

### Platform Priority Ranking (for civic accountability)

1. **Facebook Pages** — Already handled by FB-Monitor. Most elected officials post here. Keep and improve.
2. **YouTube** — Excellent free API. Many agencies post meeting recordings and press conferences. High value, low cost.
3. **Bluesky** — Growing adoption among public officials. Fully open protocol. Trivial to add.
4. **X/Twitter** — Still significant for public officials, but $200-5000/mo for useful access. Cost is the main barrier.
5. **Mastodon** — Some government agencies have Mastodon accounts. Free API. Worth adding if targets exist.
6. **Instagram** — Mostly visual content. Limited API access for third-party monitoring. Low priority unless specific targets.
7. **TikTok** — Research API has comments but requires university affiliation. Low priority unless you qualify.
8. **Threads** — API improving (keyword search, public profiles since 2025). Monitor for future expansion.

---

## PART 3 — DIY Architecture Proposal

### Current FB-Monitor Architecture vs. Proposed Multi-Platform Architecture

```
CURRENT (FB-Monitor)                    PROPOSED (Social Media Archive)
========================                ================================

config.json                             sources.yaml
  └── pages[]                             ├── facebook_pages[]
                                          ├── youtube_channels[]
fb_monitor.py (monolith)                  ├── bluesky_accounts[]
  ├── Phase 1: detect posts               ├── x_accounts[]
  ├── Phase 2: recheck comments           └── mastodon_accounts[]
  └── Phase 3: process imports
                                        collectors/
extractors.py (5 strategies)              ├── base.py (abstract collector)
post_parser.py (3 strategies)             ├── facebook.py (current FB-Monitor)
comments.py (3 strategies)                ├── youtube.py (Data API v3)
collector.py + injected_collector.js      ├── bluesky.py (AT Protocol)
                                          ├── x_twitter.py (API v2)
database.py (14 tables)                   ├── mastodon.py (REST API)
                                          └── manual_ingest.py (CSV/JSON import)
tracker.py (state.json)
                                        normalizer.py
web_ui.py (FastAPI)                       └── maps all sources → common schema

                                        storage/
                                          ├── database.py (expanded schema)
                                          └── migrations/

                                        scheduler.py
                                          ├── near-realtime (1-6hr)
                                          ├── lookback_24h
                                          ├── lookback_7d
                                          └── lookback_30d

                                        integrity.py
                                          ├── content_hash()
                                          ├── detect_edits()
                                          └── tombstone_deletes()

                                        web_ui.py (expanded FastAPI)
```

### 3.1 Collectors (Per Platform)

Each collector implements a common interface:

```python
class BaseCollector(ABC):
    """Abstract base for all platform collectors."""

    @abstractmethod
    def fetch_posts(self, source_id: str, since: datetime) -> list[NormalizedPost]:
        """Fetch posts from a source since the given timestamp."""

    @abstractmethod
    def fetch_comments(self, post_id: str, since: datetime) -> list[NormalizedComment]:
        """Fetch comments on a post since the given timestamp."""

    @abstractmethod
    def get_media_urls(self, post: NormalizedPost) -> list[MediaRef]:
        """Extract downloadable media URLs from a post."""

    @abstractmethod
    def get_rate_limit_status(self) -> RateLimitStatus:
        """Report current rate limit consumption."""
```

#### Facebook Collector (upgrade current FB-Monitor)

**Keep:** Everything in the current system — it works. The multi-strategy extraction, Tor pool racing, anti-detection, and JS injection are all battle-tested.

**Improve:**
- Extract comment IDs from DOM (`data-commentid` attributes, or construct from URL fragments)
- Parse reply nesting depth from DOM structure (the `_strategy_aria` code already detects `isReply` via nested `<li>` — extend to capture parent comment reference)
- Add content hashing on save (SHA-256 of `text + sorted(media_urls)`)
- Add `first_seen_at` / `last_seen_at` / `content_hash` columns
- Add RSS fallback: some Facebook Pages have RSS feeds at `facebook.com/{page}/posts?_fb_noscript=1` or via third-party RSS bridges

**Current comment extraction gap analysis:**
The `Comment` dataclass in `comments.py:23-29` has: `author`, `text`, `timestamp`, `is_reply`, `strategy`. It's missing:
- `comment_id` (platform-native ID)
- `parent_id` (for threading)
- `depth` (nesting level)
- `reaction_count`
- `profile_url` (author's profile link)

The DB schema (`comments` table, `database.py:61-70`) stores: `post_id`, `author`, `text`, `timestamp`, `is_reply`, `detected_at`. Dedup is `UNIQUE(post_id, author, text)` — this means if someone edits a comment, the old and new versions both get stored as separate rows. No way to link them.

#### YouTube Collector (new, API-based)

```python
class YouTubeCollector(BaseCollector):
    """YouTube Data API v3 collector."""
    # GET /channels?part=contentDetails — get uploads playlist
    # GET /playlistItems?playlistId={uploads} — list videos
    # GET /videos?part=snippet,statistics — video details
    # GET /commentThreads?videoId={id}&part=snippet,replies — threaded comments!
```

YouTube is the easiest win. The Data API v3 gives you:
- Full video metadata (title, description, publish date, thumbnail URLs)
- **Threaded comments** with `parentId` — exactly what FB-Monitor is missing for Facebook
- Comment author details (channel name, profile image)
- Reaction counts (likes on comments)
- Pagination via `nextPageToken`
- 10,000 quota units/day free (each list request costs 1 unit, each search costs 100)

**Quota budget for monitoring 20 channels:**
- Channel check: 1 unit × 20 = 20 units
- Video details for new videos: ~1 unit × ~10 new/day = 10 units
- Comment threads (100 per page): 1 unit × ~50 pages/day = 50 units
- Total: ~80 units/day out of 10,000. Extremely comfortable.

#### Bluesky Collector (new, AT Protocol)

```python
class BlueskyCollector(BaseCollector):
    """Bluesky AT Protocol collector."""
    # GET /xrpc/app.bsky.feed.getAuthorFeed?actor={handle}
    # GET /xrpc/app.bsky.feed.getPostThread?uri={post_uri}
    # Posts have stable AT URIs (at://did:plc:xxx/app.bsky.feed.post/yyy)
    # Comments are just posts with reply references
```

Bluesky is fully open:
- No API key needed for public data reads
- Posts have stable URIs (`at://` scheme) perfect for dedup
- "Comments" are reply posts — `getPostThread` returns the full tree
- Media is stored as blobs with stable CIDs
- Rate limits are generous (3000 pts/5min for unauthenticated)

#### X/Twitter Collector (new, API v2)

```python
class XCollector(BaseCollector):
    """X API v2 collector."""
    # GET /2/users/{id}/tweets — user timeline
    # GET /2/tweets/search/recent?query=from:{user} — search
    # GET /2/tweets/{id}?expansions=author_id&tweet.fields=... — tweet detail
    # Replies: GET /2/tweets/search/recent?query=conversation_id:{id}
```

X/Twitter is functional but expensive:
- Basic ($200/mo): 10,000 tweet reads/mo, 1 app. Marginal for monitoring many accounts.
- Pro ($5,000/mo): 1M tweet reads/mo, full archive search. Realistic for serious archival.
- Tweet objects include `conversation_id` for threading and `referenced_tweets` for quote tweets/retweets.
- Media URLs are directly in `media` expansions.
- Comments (replies) require searching by `conversation_id`.

#### Mastodon Collector (new, REST API)

```python
class MastodonCollector(BaseCollector):
    """Mastodon REST API collector (per-instance)."""
    # GET /api/v1/accounts/{id}/statuses — account timeline
    # GET /api/v1/statuses/{id}/context — thread (ancestors + descendants)
    # Media attachments are directly in status objects
```

Simple, generous API:
- 300 requests per 5 minutes (no auth needed for public data)
- Full thread context with one call
- Media URLs directly in response
- Challenge: federated = need to track which instance each account is on

#### Manual Ingest Collector

```python
class ManualIngestCollector(BaseCollector):
    """Import from exports, CSV files, JSON dumps, or browser extension."""
    # Accepts: CSV with columns (platform, url, text, author, timestamp)
    # Accepts: JSON matching the NormalizedPost schema
    # Accepts: Browser extension payloads (current /api/ingest endpoint)
    # Accepts: Facebook Page data exports (Settings > Your Facebook Information)
```

This is the safety valve for platforms with no API access. FB-Monitor already has `/api/ingest` and the import queue — extend this to accept multi-platform data.

### 3.2 Normalizer

All collectors output a common schema:

```python
@dataclass
class NormalizedPost:
    platform: str           # "facebook", "youtube", "bluesky", "x", "mastodon"
    platform_id: str        # Platform-native unique ID
    source_id: str          # Page/channel/account being monitored
    source_name: str        # Human-readable source name
    url: str                # Canonical URL to the post
    author_name: str        # Display name of author
    author_url: str         # Profile URL
    text: str               # Post body text
    timestamp: datetime     # When the post was published (UTC)
    timestamp_raw: str      # Original timestamp string from platform
    media: list[MediaRef]   # Images, videos, thumbnails
    links: list[str]        # External links in the post
    reaction_count: int     # Likes/reactions (0 if unavailable)
    comment_count: int      # Comment count (0 if unavailable)
    share_count: int        # Shares/retweets/reposts (0 if unavailable)
    is_shared: bool         # Whether this is a share/repost/retweet
    shared_from: str        # Original author if shared
    shared_url: str         # Original post URL if shared
    raw_data: dict          # Full platform-native response (for debugging)
    content_hash: str       # SHA-256 of (text + sorted media URLs)
    first_seen_at: datetime # When we first collected this
    last_seen_at: datetime  # Most recent collection

@dataclass
class NormalizedComment:
    platform: str
    platform_id: str        # Platform-native comment ID
    post_platform_id: str   # Parent post's platform ID
    parent_comment_id: str  # Parent comment ID (empty for top-level)
    root_comment_id: str    # Root of this thread (= self if top-level)
    depth: int              # 0 = top-level, 1 = first reply, etc.
    author_name: str
    author_url: str
    text: str
    timestamp: datetime
    reaction_count: int
    content_hash: str
    first_seen_at: datetime
    last_seen_at: datetime
    is_deleted: bool        # Tombstone flag

@dataclass
class MediaRef:
    type: str               # "image", "video", "thumbnail"
    url: str                # CDN/source URL
    local_path: str         # Local download path (empty until downloaded)
    width: int
    height: int
    content_hash: str       # SHA-256 of downloaded file (empty until downloaded)
```

**Mapping from current FB-Monitor fields to NormalizedPost:**

| Current field | Normalized field | Notes |
|---------------|-----------------|-------|
| `post_id` | `platform_id` | Already extracted, works well |
| `page_name` | `source_name` | Direct mapping |
| `page_url` | (derive `source_id`) | Use page slug as source_id |
| `post_url` | `url` | Direct mapping |
| `author` | `author_name` | Direct mapping |
| `text` | `text` | After sanitization (already done) |
| `timestamp` | `timestamp` | Needs parsing (currently stored as string) |
| `timestamp_raw` | `timestamp_raw` | Direct mapping |
| `shared_from` | `shared_from` | Direct mapping |
| `reaction_count` | `reaction_count` | Currently string, needs int parse |
| `comment_count_text` | `comment_count` | Currently string like "12 Comments" |
| `share_count_text` | `share_count` | Currently string like "3 Shares" |
| `image_urls` | `media[]` | Convert to MediaRef list |
| `video_urls` | `media[]` | Convert to MediaRef list |
| (missing) | `content_hash` | **Add:** SHA-256 of text + media URLs |
| `detected_at` | `first_seen_at` | Direct mapping |
| (missing) | `last_seen_at` | **Add:** update on each re-fetch |

### 3.3 Storage

#### Recommended Schema (SQLite for MVP, PostgreSQL for production)

The current 14-table schema is a good foundation. Here's the expanded schema:

```sql
-- Sources: pages/channels/accounts being monitored
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,          -- 'facebook', 'youtube', 'bluesky', etc.
    platform_id TEXT NOT NULL,       -- Platform-native ID (page ID, channel ID, DID)
    name TEXT NOT NULL,              -- Human-readable name
    url TEXT NOT NULL,               -- Canonical URL
    enabled INTEGER DEFAULT 1,
    poll_interval_minutes INTEGER DEFAULT 60,
    last_fetched_at TEXT,
    config_json TEXT,                -- Platform-specific config (API keys, etc.)
    created_at TEXT NOT NULL,
    UNIQUE(platform, platform_id)
);

-- Posts: the core table
CREATE TABLE posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,       -- Platform-native post ID (stable)
    source_id INTEGER NOT NULL REFERENCES sources(id),
    url TEXT NOT NULL,
    author_name TEXT,
    author_url TEXT,
    text TEXT,
    timestamp TEXT,                  -- ISO 8601 UTC
    timestamp_raw TEXT,
    is_shared INTEGER DEFAULT 0,
    shared_from TEXT,
    shared_url TEXT,
    links_json TEXT,                 -- JSON array of external links
    reaction_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    content_hash TEXT,               -- SHA-256(text + sorted media URLs)
    raw_json TEXT,                   -- Full platform response for debugging
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    is_deleted INTEGER DEFAULT 0,    -- Tombstone: was visible, now gone
    deleted_at TEXT,
    UNIQUE(platform, platform_id)
);

-- Post versions: track edits
CREATE TABLE post_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    text TEXT,
    content_hash TEXT,
    links_json TEXT,
    reaction_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    captured_at TEXT NOT NULL
);

-- Comments: with full threading support
CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_id TEXT,                -- Platform-native comment ID (nullable for FB scraping)
    post_id INTEGER NOT NULL REFERENCES posts(id),
    parent_comment_id INTEGER REFERENCES comments(id),  -- NULL for top-level
    root_comment_id INTEGER REFERENCES comments(id),    -- Self-ref for top-level
    depth INTEGER DEFAULT 0,
    author_name TEXT,
    author_url TEXT,
    text TEXT NOT NULL,
    timestamp TEXT,
    reaction_count INTEGER DEFAULT 0,
    content_hash TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    is_deleted INTEGER DEFAULT 0,
    deleted_at TEXT,
    UNIQUE(platform, platform_id) -- For platforms that give comment IDs
);
-- Fallback uniqueness for Facebook (no reliable comment IDs):
CREATE UNIQUE INDEX idx_comments_fb_dedup
    ON comments(post_id, author_name, content_hash)
    WHERE platform = 'facebook' AND platform_id IS NULL;

-- Comment versions: track edits
CREATE TABLE comment_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id INTEGER NOT NULL REFERENCES comments(id),
    text TEXT,
    content_hash TEXT,
    captured_at TEXT NOT NULL
);

-- Media/attachments
CREATE TABLE media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    type TEXT NOT NULL,              -- 'image', 'video', 'thumbnail'
    url TEXT,                        -- Source URL
    local_path TEXT,                 -- Downloaded file path
    filename TEXT,
    width INTEGER,
    height INTEGER,
    file_hash TEXT,                  -- SHA-256 of downloaded file
    downloaded_at TEXT,
    download_error TEXT
);

-- Fetch runs: audit trail
CREATE TABLE fetch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    collector TEXT NOT NULL,         -- 'facebook', 'youtube', etc.
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT DEFAULT 'running',   -- 'running', 'success', 'partial', 'failed'
    posts_found INTEGER DEFAULT 0,
    posts_new INTEGER DEFAULT 0,
    comments_found INTEGER DEFAULT 0,
    comments_new INTEGER DEFAULT 0,
    error TEXT,
    rate_limit_remaining INTEGER
);

-- Errors: structured error log
CREATE TABLE errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_run_id INTEGER REFERENCES fetch_runs(id),
    source_id INTEGER REFERENCES sources(id),
    platform TEXT,
    error_type TEXT,                 -- 'rate_limit', 'auth', 'parse', 'network', 'api'
    message TEXT,
    url TEXT,
    occurred_at TEXT NOT NULL
);

-- Keep existing people/entities/categories tables from FB-Monitor
-- (people, people_pages, people_posts, people_comments,
--  entities, entity_pages, entity_people,
--  categories, post_categories)

-- Import queue (expanded from current)
CREATE TABLE import_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL DEFAULT 'facebook',
    url TEXT NOT NULL,
    source_name TEXT,
    status TEXT DEFAULT 'pending',
    post_id INTEGER REFERENCES posts(id),
    error TEXT,
    submitted_at TEXT NOT NULL,
    processed_at TEXT,
    UNIQUE(platform, url)
);

-- Indexes
CREATE INDEX idx_posts_platform ON posts(platform);
CREATE INDEX idx_posts_source ON posts(source_id);
CREATE INDEX idx_posts_timestamp ON posts(timestamp);
CREATE INDEX idx_posts_first_seen ON posts(first_seen_at);
CREATE INDEX idx_posts_content_hash ON posts(content_hash);
CREATE INDEX idx_comments_post ON comments(post_id);
CREATE INDEX idx_comments_parent ON comments(parent_comment_id);
CREATE INDEX idx_comments_root ON comments(root_comment_id);
CREATE INDEX idx_comments_content_hash ON comments(content_hash);
CREATE INDEX idx_media_post ON media(post_id);
CREATE INDEX idx_fetch_runs_source ON fetch_runs(source_id);
CREATE INDEX idx_errors_source ON errors(source_id);
```

**Key changes from current schema:**
1. `sources` table replaces `config.json` page list — supports multiple platforms
2. `posts` and `comments` gain `platform`, `platform_id`, `content_hash`, `first_seen_at`, `last_seen_at`, `is_deleted`
3. `post_versions` and `comment_versions` tables for edit tracking
4. `comments` gain `parent_comment_id`, `root_comment_id`, `depth` for threading
5. `fetch_runs` and `errors` tables for audit trail
6. `media` table replaces `attachments` with `file_hash` for dedup

**Migration from current schema:** The existing 14 tables can coexist. Add the new tables alongside, and write a one-time migration script that:
1. Creates `sources` entries from `config.json` pages (all `platform='facebook'`)
2. Adds `platform`, `content_hash`, `first_seen_at`, `last_seen_at` columns to existing `posts` table
3. Backfills `content_hash` from existing text
4. Keeps `people`, `entities`, `categories` tables as-is

### 3.4 Scheduler

**Current FB-Monitor scheduling:**
- `feed_poll_minutes: 3` — feed extraction every 3 minutes (aggressive, via Tor)
- `check_interval_minutes: 15` — full run_cycle every 15 minutes
- `comment_recheck_interval_minutes: 30` — recheck comments every 30 minutes
- `comment_tracking_hours: 24` — track comments for 24 hours after post detection
- Logged-in accounts: 90-minute comment recheck interval

This is good for near-real-time Facebook monitoring but doesn't account for late comments or long-term changes.

**Proposed tiered scheduling:**

```python
SCHEDULE_TIERS = {
    "realtime": {
        "interval_minutes": 15,      # Check every 15 min
        "applies_to": "all_sources",
        "description": "Detect new posts",
    },
    "comment_active": {
        "interval_minutes": 30,      # Recheck every 30 min
        "window_hours": 24,          # For 24 hours after post
        "description": "Active comment tracking window",
    },
    "lookback_48h": {
        "interval_hours": 6,         # Check every 6 hours
        "applies_after_hours": 24,   # After active window
        "applies_until_hours": 48,
        "description": "Catch late comments in first 48h",
    },
    "lookback_7d": {
        "interval_hours": 24,        # Check once per day
        "applies_after_hours": 48,
        "applies_until_hours": 168,  # 7 days
        "description": "Daily check for first week",
    },
    "lookback_30d": {
        "interval_hours": 168,       # Check once per week
        "applies_after_hours": 168,
        "applies_until_hours": 720,  # 30 days
        "description": "Weekly check for first month",
    },
    "integrity_sweep": {
        "interval_hours": 720,       # Monthly
        "applies_to": "all_posts",
        "description": "Monthly full rescan to detect edits/deletes",
    },
}
```

**Per-platform scheduling considerations:**

| Platform | Optimal poll frequency | Reason |
|----------|----------------------|--------|
| Facebook (Tor/anon) | 3 min feed polls (current) | Tor circuits are cheap, login walls are the bottleneck |
| Facebook (logged-in) | 90 min | Conservative to avoid account flags |
| YouTube | 1-6 hours | API quota is generous, videos don't appear as frequently |
| Bluesky | 5-15 min | No rate limit concerns, fast API |
| X/Twitter | Depends on tier | Basic: hourly at most (10K/mo budget). Pro: every 15 min |
| Mastodon | 15-30 min | 300 req/5min is generous |

### 3.5 Dedupe + Integrity

**Current FB-Monitor dedup:**
- Posts: `UNIQUE(post_id)` on the `posts` table — good, uses platform-native IDs
- Comments: `UNIQUE(post_id, author, text)` — fragile, breaks on edits/name changes
- Seen posts: `state.json` → `seen_posts[page_key]` list of post IDs — works but redundant with DB
- No content hashing, no edit detection, no delete detection

**Proposed integrity system:**

```python
import hashlib

def content_hash(text: str, media_urls: list[str]) -> str:
    """Generate a stable hash of post/comment content for edit detection."""
    normalized = text.strip().lower()
    media_part = "|".join(sorted(media_urls))
    payload = f"{normalized}\n{media_part}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

def detect_edit(existing_hash: str, new_hash: str) -> bool:
    """True if content has changed since last fetch."""
    return existing_hash != new_hash and existing_hash != ""

def handle_content_change(post_or_comment, new_data):
    """When content hash changes: save version, update hash."""
    # 1. Insert current state into post_versions/comment_versions
    # 2. Update the main record with new content + hash
    # 3. Update last_seen_at
    pass

def detect_deletion(post_or_comment, fetch_result):
    """When previously-seen content is missing from fetch results."""
    # Don't tombstone immediately — could be pagination issue
    # Mark as "possibly_deleted" after 2 consecutive misses
    # Mark as "deleted" after 3 consecutive misses or 48 hours
    pass
```

**Unique ID strategy by platform:**

| Platform | Post ID | Comment ID | Stability |
|----------|---------|------------|-----------|
| Facebook | `pfbid...` or numeric from URL | None from scraping (construct from author+text hash) | Posts: good. Comments: poor |
| YouTube | `videoId` from API | `commentId` from API | Excellent |
| Bluesky | `at://` URI | `at://` URI (replies are posts) | Excellent |
| X/Twitter | Tweet ID (numeric) | Tweet ID (replies are tweets) | Excellent |
| Mastodon | Status ID (per-instance) | Status ID (replies are statuses) | Good (instance-scoped) |

**Facebook comment ID problem and workaround:**

The current system has no platform-native comment IDs because Facebook's DOM doesn't reliably expose them for scraped content. Proposed workaround:

```python
def generate_fb_comment_id(post_id: str, author: str, text: str, timestamp: str) -> str:
    """Generate a synthetic stable ID for Facebook comments."""
    # Use content hash as the ID — will change if text is edited,
    # but that's actually desirable (creates a new version)
    payload = f"{post_id}|{author}|{text[:200]}|{timestamp}"
    return f"fb_cmt_{hashlib.sha256(payload.encode()).hexdigest()[:12]}"
```

This isn't perfect (edits create new IDs), but combined with the `content_hash` + `first_seen_at` audit trail, you can reconstruct the timeline of what was said and when.

---

## PART 4 — Comment/Thread Strategy

### Current FB-Monitor Comment Handling

**What works:**
- 3 extraction strategies with fallback (`_strategy_aria`, `_strategy_mobile`, `_strategy_text_blocks`)
- Comment expansion: clicks "View more comments" and "See more replies" up to 15 times (`_expand_comments`)
- Merge logic: `merge_comments()` in `comments.py` deduplicates by `(author, text)` tuple
- 24-hour tracking window with 30-minute recheck intervals
- Writes to both JSON files and SQLite

**What doesn't work well:**
- `is_reply` is a boolean — can't reconstruct who replied to whom
- No comment IDs — can't track individual comments across rechecks
- Dedup by `(author, text)` means edited comments appear as new
- No depth tracking — can't distinguish reply-to-reply from top-level reply
- Comment expansion clicks have a fixed limit (15) — may miss deeply threaded conversations
- No pagination tracking — can't resume from where we left off

### Proposed Thread Storage Model

```sql
-- Example data for a threaded conversation:

-- Top-level comment (depth 0):
INSERT INTO comments VALUES (
    1,                          -- id
    'facebook',                 -- platform
    NULL,                       -- platform_id (FB doesn't give us one)
    42,                         -- post_id (FK to posts table)
    NULL,                       -- parent_comment_id (top-level)
    1,                          -- root_comment_id (self-reference)
    0,                          -- depth
    'John Smith',               -- author_name
    'https://facebook.com/john',-- author_url
    'This is outrageous!',      -- text
    '2026-02-24T10:30:00Z',    -- timestamp
    5,                          -- reaction_count
    'a1b2c3d4',                 -- content_hash
    '2026-02-24T11:00:00Z',    -- first_seen_at
    '2026-02-24T15:00:00Z',    -- last_seen_at
    0,                          -- is_deleted
    NULL                        -- deleted_at
);

-- Reply to John (depth 1):
INSERT INTO comments VALUES (
    2, 'facebook', NULL, 42,
    1,                          -- parent_comment_id = John's comment
    1,                          -- root_comment_id = John's comment (thread root)
    1,                          -- depth = 1
    'Jane Doe', 'https://facebook.com/jane',
    'I agree completely.',
    '2026-02-24T10:45:00Z', 2, 'e5f6g7h8',
    '2026-02-24T11:00:00Z', '2026-02-24T15:00:00Z', 0, NULL
);

-- Reply to Jane (depth 2):
INSERT INTO comments VALUES (
    3, 'facebook', NULL, 42,
    2,                          -- parent_comment_id = Jane's comment
    1,                          -- root_comment_id = still John's comment
    2,                          -- depth = 2
    'Bob Wilson', 'https://facebook.com/bob',
    'Me too, this needs to change.',
    '2026-02-24T11:00:00Z', 1, 'i9j0k1l2',
    '2026-02-24T11:30:00Z', '2026-02-24T15:00:00Z', 0, NULL
);
```

**Querying a full thread tree:**
```sql
-- Get all comments in a thread, ordered for display:
SELECT * FROM comments
WHERE root_comment_id = 1
ORDER BY depth, timestamp;

-- Get top-level comments for a post:
SELECT * FROM comments
WHERE post_id = 42 AND depth = 0
ORDER BY timestamp;

-- Get direct replies to a comment:
SELECT * FROM comments
WHERE parent_comment_id = 1
ORDER BY timestamp;

-- Get full thread count per root comment:
SELECT root_comment_id, COUNT(*) as thread_size
FROM comments
WHERE post_id = 42
GROUP BY root_comment_id;
```

### Pagination Strategy + Incremental Updates

**Current approach (FB-Monitor):**
The `_expand_comments` function clicks "View more" buttons up to 15 times, then extracts everything visible on the page. This is a "load everything" approach — simple but doesn't scale.

**Proposed incremental approach:**

```python
class CommentFetcher:
    """Incremental comment fetching with pagination tracking."""

    def fetch_comments_incremental(self, post_id: str, last_fetch_state: dict) -> tuple[list, dict]:
        """
        Fetch comments incrementally.

        For API-based platforms (YouTube, Bluesky, X):
          - Use cursor/pageToken from last_fetch_state
          - Only fetch new pages since last check

        For Facebook (scraping):
          - Still need to load all visible comments (no cursor)
          - But compare against existing DB entries by content_hash
          - Only save genuinely new/changed comments

        Returns: (new_comments, updated_fetch_state)
        """
```

**Per-platform pagination:**

| Platform | Pagination method | Incremental? | Strategy |
|----------|------------------|--------------|----------|
| Facebook | Click "View more" (DOM) | No — must re-expand each time | Fetch all, diff against DB by hash |
| YouTube | `nextPageToken` cursor | Yes | Store pageToken, resume from last position |
| Bluesky | `cursor` parameter | Yes | Store cursor, fetch only new |
| X/Twitter | `pagination_token` | Yes | Store token, use `since_id` for efficiency |
| Mastodon | `min_id` / `max_id` Link headers | Yes | Use `since_id` parameter |

### Handling Edits and Deletes

**Edit detection flow:**
```
1. Fetch comments for post
2. For each comment:
   a. Compute content_hash(text)
   b. Look up existing comment by (platform, platform_id) or fallback (post_id, author, old_hash)
   c. If found AND hash differs:
      - Insert into comment_versions (old text, old hash, timestamp)
      - Update comment with new text, new hash, new last_seen_at
   d. If found AND hash matches:
      - Update last_seen_at only
   e. If not found:
      - Insert as new comment
```

**Delete detection flow:**
```
1. After fetching all visible comments for a post:
2. For each comment in DB that was NOT seen in this fetch:
   a. Increment a "consecutive_misses" counter (stored in fetch metadata)
   b. If consecutive_misses >= 3 AND last_seen > 48 hours ago:
      - Set is_deleted = 1, deleted_at = now
      - This is a "tombstone" — the row stays, marked as deleted
   c. If consecutive_misses < 3:
      - Could be pagination issue, don't tombstone yet
3. Never actually DELETE rows — tombstones preserve the audit trail
```

**Why tombstones matter for accountability:**
When a public official deletes a comment (theirs or a constituent's), that deletion itself is newsworthy. The tombstone preserves:
- What was said (`text`)
- Who said it (`author_name`)
- When it first appeared (`first_seen_at`)
- When it was last confirmed present (`last_seen_at`)
- When we detected the deletion (`deleted_at`)
- Previous versions if edited before deletion (`comment_versions`)

---

## PART 5 — Comparison: What FB-Monitor Does Well vs. What Needs Work

### Scorecard

| Capability | Current FB-Monitor | Proposed System | Priority |
|------------|-------------------|-----------------|----------|
| Facebook post detection | A | A (keep as-is) | - |
| Facebook comment extraction | B- | A (add threading, IDs) | High |
| Anti-detection / stealth | A | A (keep as-is) | - |
| Tor pool management | A | A (keep, FB-only) | - |
| Multi-platform support | F | B+ (add 4 platforms) | High |
| Comment threading | D (boolean only) | A (full tree) | High |
| Edit detection | F | A (content hashing) | High |
| Delete detection | F | B+ (tombstones) | Medium |
| Audit trail | D (detected_at only) | A (first/last seen, versions) | High |
| Dedup reliability | C+ | A (platform IDs + hashes) | High |
| Scheduling sophistication | B | A (tiered lookbacks) | Medium |
| Media archival | B+ | B+ (add file hashing) | Low |
| Data integrity | C | A (hashes, versions, tombstones) | High |
| API/REST interface | A- | A (extend for multi-platform) | Low |
| Web UI | B+ | B+ (extend for multi-platform) | Low |

### Implementation Order (Recommended)

**Phase 1: Strengthen Facebook (the core)**
1. Add `content_hash`, `first_seen_at`, `last_seen_at` to posts and comments
2. Add `parent_comment_id`, `root_comment_id`, `depth` to comments
3. Add `post_versions` and `comment_versions` tables
4. Add `is_deleted` / `deleted_at` tombstone support
5. Implement edit/delete detection in the recheck loop
6. Add lookback tiers (48h, 7d, 30d) to the scheduler

**Phase 2: Add YouTube (highest ROI new platform)**
7. YouTube collector using Data API v3
8. Normalizer mapping YouTube → common schema
9. Store in same DB with `platform='youtube'`
10. Extend web UI to show YouTube content

**Phase 3: Add Bluesky (easiest, growing adoption)**
11. Bluesky collector using AT Protocol
12. Same normalizer/storage pattern

**Phase 4: Add X/Twitter (if budget allows)**
13. X collector using API v2
14. Budget monitoring for API usage

**Phase 5: Add Mastodon (if targets exist)**
15. Mastodon collector (per-instance)
16. Instance discovery/tracking

---

## Appendix: Current FB-Monitor File-by-File Analysis

| File | Lines | Role | Quality | Notes |
|------|-------|------|---------|-------|
| `fb_monitor.py` | ~1500 | Main scraper orchestrator | Good | Well-structured 3-phase cycle. Feed poll + full cycle modes. Thread pool for concurrent page processing. |
| `web_ui.py` | ~1000+ | FastAPI web UI + REST API | Good | Clean API design, good Atlas integration endpoints. |
| `database.py` | ~500+ | SQLite schema + CRUD | Good | 14 tables, proper indexes, WAL mode. Needs content_hash + threading columns. |
| `extractors.py` | ~300 | 5-strategy post extraction | Very Good | Resilient fallback chain with health tracking. Well-designed. |
| `post_parser.py` | ~300 | 3-strategy post data parsing | Good | Structured DOM, mobile fallback, JSON-LD. |
| `comments.py` | ~300 | 3-strategy comment extraction | Good (structure) / Weak (data model) | Extraction logic is solid. Data model (no IDs, no threading) is the weak point. |
| `collector.py` | ~200 | JS injection bridge | Good | Orchestrates the injected_collector.js pipeline. |
| `sanitize.py` | ~300 | Data cleaning + validation | Very Good | Comprehensive login wall detection, chrome stripping, garbage filtering. |
| `stealth.py` | ~400 | Anti-detection measures | Excellent | Coherent fingerprints, WebGL spoofing, human-like behavior, Tor integration. |
| `sessions.py` | ~200 | Account management | Good | Clean separation of anonymous vs. logged-in. Persistent profiles. |
| `downloader.py` | ~300 | Media download | Good | 3 download modes (direct, Tor, VPS proxy). yt-dlp for video. |
| `tracker.py` | ~100 | State management | Adequate | Simple JSON state. Could be merged into DB. |
| `config.json` | ~160 | Configuration | Good | Well-structured with per-account settings. |

### Key Architectural Decisions to Preserve

1. **Multi-strategy extraction with health tracking** — This is the best design decision in the codebase. Don't abandon it for a single extraction approach.
2. **Tor pool with instance racing** — Genuinely clever engineering. Keep for Facebook.
3. **Two-tier account system** — The anonymous/logged-in split with different rate limits is the right approach.
4. **JS injection collector** — Running extraction logic inside the page context avoids many anti-scraping measures.
5. **Separate feed poll cycle** — The fast 3-minute feed poll + slower 15-minute full cycle is a good separation of concerns.

---

## Appendix B: API Research Sources

**Facebook / Meta Content Library:**
- [Meta Content Library | Transparency Center](https://transparency.meta.com/researchtools/meta-content-library)
- [Meta Content Library | SOMAR / ICPSR](https://www.icpsr.umich.edu/sites/somar/meta-content-library)
- [Meta Graph API Considerations | Data365](https://data365.co/blog/meta-graph-api)

**Instagram:**
- [Instagram Graph API Developer Guide 2026](https://elfsight.com/blog/instagram-graph-api-complete-developer-guide-for-2026/)
- [After Basic Display EOL: Instagram 2026 API Rules](https://storrito.com/resources/Instagram-API-2026/)

**X/Twitter:**
- [X API Pricing Tiers 2025](https://twitterapi.io/blog/twitter-api-pricing-2025)
- [About the X API](https://docs.x.com/x-api/getting-started/about-x-api)
- [What Happened to Academic Research on Twitter | CJR](https://www.cjr.org/tow_center/qa-what-happened-to-academic-research-on-twitter.php)

**YouTube:**
- [YouTube Data API v3 Overview](https://developers.google.com/youtube/v3/getting-started)
- [YouTube API Quota Calculator](https://developers.google.com/youtube/v3/determine_quota_cost)
- [YouTube Researcher Program](https://research.youtube/how-it-works/)

**TikTok:**
- [TikTok Research API](https://developers.tiktok.com/products/research-api/)
- [TikTok Research API Problems | AI Forensics](https://aiforensics.org/work/tk-api)

**Threads:**
- [Threads API Documentation | Postman](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api)
- [Meta Expands Threads API](https://ppc.land/meta-expands-threads-api-with-advanced-features-for-developers/)

**Bluesky:**
- [Bluesky Firehose Documentation](https://docs.bsky.app/docs/advanced-guides/firehose)
- [Bluesky Rate Limits](https://docs.bsky.app/docs/advanced-guides/rate-limits)
- [Introducing Jetstream](https://docs.bsky.app/blog/jetstream)
- [app.bsky.feed.searchPosts](https://docs.bsky.app/docs/api/app-bsky-feed-search-posts)

**Mastodon:**
- [Mastodon API Rate Limits](https://docs.joinmastodon.org/api/rate-limits/)
- [Mastodon Statuses API](https://docs.joinmastodon.org/methods/statuses/)
- [Mastodon 4.5 for Developers](https://blog.joinmastodon.org/2025/10/mastodon-4-5-for-devs/)
