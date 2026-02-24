# FB-Monitor — Data Schema & API Reference

**Last updated:** 2026-02-24

This document covers the complete data lifecycle: how data enters the system,
where it's stored, how it's queried, and how the web UI renders it.

---

## Database: `fb_monitor.db` (SQLite, WAL mode)

### Table Overview (16 tables)

| Table | Records | Purpose |
|-------|---------|---------|
| `posts` | Core | Scraped Facebook posts |
| `comments` | Core | Comments on posts (threaded, with depth) |
| `attachments` | Core | Images, videos, poster thumbnails |
| `post_versions` | Integrity | Historical snapshots of edited posts |
| `comment_versions` | Integrity | Historical snapshots of edited comments |
| `people` | Enrichment | Named individuals tracked across pages |
| `people_pages` | Enrichment | Person ↔ page links (with role) |
| `people_posts` | Enrichment | Person ↔ post links (with role) |
| `people_comments` | Enrichment | Person ↔ comment links |
| `categories` | Enrichment | Tag taxonomy for posts |
| `post_categories` | Enrichment | Post ↔ category links |
| `entities` | Enrichment | Organizations/groups |
| `entity_pages` | Enrichment | Entity ↔ page links |
| `entity_people` | Enrichment | Entity ↔ person links (with role) |
| `import_queue` | Operations | Bulk URL import queue |
| `media_queue` | Operations | Pending media downloads |

---

### Core Tables

#### `posts`

```sql
CREATE TABLE posts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id             TEXT UNIQUE NOT NULL,    -- Facebook post ID (pfbid..., numeric, etc.)
    page_name           TEXT NOT NULL,           -- Display name of the page
    page_url            TEXT,                    -- Full Facebook page URL
    post_url            TEXT NOT NULL,           -- Direct URL to this post
    author              TEXT,                    -- Post author (may differ from page for community posts)
    text                TEXT,                    -- Full post text
    timestamp           TEXT,                    -- Resolved timestamp (ISO or display format)
    timestamp_raw       TEXT,                    -- Original timestamp text from Facebook
    shared_from         TEXT,                    -- Name of original poster if shared
    shared_original_url TEXT,                    -- URL of original post if shared
    links               TEXT,                    -- JSON array of external URLs in post
    reaction_count      TEXT,                    -- e.g. "42" or "5.2K"
    comment_count_text  TEXT,                    -- e.g. "10 comments"
    share_count_text    TEXT,                    -- e.g. "3 shares"
    post_dir            TEXT,                    -- Local directory for downloaded media
    account             TEXT,                    -- Which account scraped this ("anonymous", "extension", etc.)
    detected_at         TEXT NOT NULL,           -- ISO timestamp when first scraped
    -- Phase 1 integrity fields:
    content_hash        TEXT,                    -- SHA-256[:16] of text + sorted media URLs
    first_seen_at       TEXT,                    -- When content was first captured
    last_seen_at        TEXT,                    -- When content was last confirmed visible
    is_deleted          INTEGER DEFAULT 0,       -- 1 = tombstoned (no longer accessible)
    deleted_at          TEXT                     -- When deletion was detected
);
```

**Key indexes:** `post_id` (unique), `content_hash`, `first_seen_at`, `is_deleted`

#### `comments`

```sql
CREATE TABLE comments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id             TEXT NOT NULL REFERENCES posts(post_id),
    author              TEXT,                    -- Comment author name
    text                TEXT NOT NULL,           -- Comment text (max 2000 chars)
    timestamp           TEXT,                    -- Relative ("2h") or resolved timestamp
    is_reply            INTEGER DEFAULT 0,       -- 1 = this is a reply to another comment
    detected_at         TEXT NOT NULL,           -- ISO timestamp when first scraped
    -- Phase 1 integrity fields:
    content_hash        TEXT,                    -- SHA-256[:16] of text
    first_seen_at       TEXT,                    -- When first captured
    last_seen_at        TEXT,                    -- When last confirmed visible
    is_deleted          INTEGER DEFAULT 0,       -- 1 = tombstoned
    deleted_at          TEXT,                    -- When deletion detected
    -- Threading fields:
    parent_comment_id   INTEGER REFERENCES comments(id),  -- Direct parent (not yet populated)
    root_comment_id     INTEGER REFERENCES comments(id),  -- Top-level ancestor (not yet populated)
    depth               INTEGER DEFAULT 0,       -- 0=top-level, 1=reply, 2=reply-to-reply, etc.
    UNIQUE(post_id, author, text)
);
```

**Key indexes:** `post_id`, `content_hash`, `parent_comment_id`, `root_comment_id`, `is_deleted`

**Dedup:** Uniqueness is `(post_id, author, text)`. If the same author posts the
same text on the same post, it's treated as a duplicate. On conflict, `last_seen_at`
and `content_hash` are updated.

**Note on parent_comment_id/root_comment_id:** These columns exist but are not
yet populated by the extraction pipeline. Facebook doesn't expose stable comment
IDs in the public DOM, so parent/root linking requires heuristic matching. The
`depth` field IS populated by both the scraper and the Tampermonkey script.

#### `attachments`

```sql
CREATE TABLE attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         TEXT NOT NULL REFERENCES posts(post_id),
    type            TEXT NOT NULL,       -- "image", "video", "poster" (video thumbnail)
    url             TEXT,                -- Original CDN URL
    local_path      TEXT,                -- Path to downloaded file (if downloaded)
    filename        TEXT,                -- Filename
    downloaded_at   TEXT                 -- When downloaded
);
```

#### `post_versions`

```sql
CREATE TABLE post_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id             TEXT NOT NULL REFERENCES posts(post_id),
    text                TEXT,            -- Text at time of capture
    content_hash        TEXT,            -- Hash at time of capture
    links               TEXT,            -- JSON array of links at time of capture
    reaction_count      TEXT,
    comment_count_text  TEXT,
    share_count_text    TEXT,
    captured_at         TEXT NOT NULL    -- When this version was archived
);
```

**Created automatically** when `save_post()` detects a content_hash change.

#### `comment_versions`

```sql
CREATE TABLE comment_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id      INTEGER NOT NULL REFERENCES comments(id),
    text            TEXT,
    content_hash    TEXT,
    captured_at     TEXT NOT NULL
);
```

---

### Enrichment Tables

#### `people`

```sql
CREATE TABLE people (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    facebook_url    TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL
);
```

#### `people_pages` / `people_posts` / `people_comments`

```sql
-- Person ↔ Page
CREATE TABLE people_pages (
    person_id   INTEGER REFERENCES people(id),
    page_name   TEXT NOT NULL,
    role        TEXT DEFAULT 'owner',  -- owner, admin, contributor, etc.
    UNIQUE(person_id, page_name)
);

-- Person ↔ Post
CREATE TABLE people_posts (
    person_id   INTEGER REFERENCES people(id),
    post_id     TEXT NOT NULL REFERENCES posts(post_id),
    role        TEXT DEFAULT 'mentioned',  -- author, tagged, mentioned, replied_to, associated
    UNIQUE(person_id, post_id, role)
);

-- Person ↔ Comment
CREATE TABLE people_comments (
    person_id   INTEGER REFERENCES people(id),
    comment_id  INTEGER NOT NULL REFERENCES comments(id),
    UNIQUE(person_id, comment_id)
);
```

#### `categories` / `post_categories`

```sql
CREATE TABLE categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    color       TEXT DEFAULT '#4f8ff7',
    created_at  TEXT NOT NULL
);

CREATE TABLE post_categories (
    post_id     TEXT NOT NULL REFERENCES posts(post_id),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    UNIQUE(post_id, category_id)
);
```

#### `entities` / `entity_pages` / `entity_people`

```sql
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE entity_pages (
    entity_id   INTEGER REFERENCES entities(id),
    page_name   TEXT NOT NULL,
    UNIQUE(entity_id, page_name)
);

CREATE TABLE entity_people (
    entity_id   INTEGER REFERENCES entities(id),
    person_id   INTEGER REFERENCES people(id),
    role        TEXT DEFAULT 'member',
    UNIQUE(entity_id, person_id)
);
```

---

### Operations Tables

#### `import_queue`

```sql
CREATE TABLE import_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL UNIQUE,
    page_name   TEXT,
    status      TEXT DEFAULT 'pending',  -- pending, scraped, failed, duplicate
    error       TEXT,
    created_at  TEXT NOT NULL,
    scraped_at  TEXT
);
```

#### `media_queue`

```sql
CREATE TABLE media_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     TEXT NOT NULL REFERENCES posts(post_id),
    type        TEXT NOT NULL,           -- "image" or "video"
    url         TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',  -- pending, downloaded, skipped
    local_path  TEXT,
    account     TEXT,
    created_at  TEXT NOT NULL
);
```

---

## Data Flow: Ingestion → Storage → Display

### Path 1: Scraper (`fb_monitor.py`)

```
Facebook Page Feed
    │
    ▼
extractors.py: extract_posts(page)
    │  Returns: list[ExtractedPost] (url, id, text, strategy)
    ▼
post_parser.py: parse_post(page, post_url)
    │  Returns: PostData (full structured post)
    ▼
sanitize.py: sanitize_post(post_dict, page_name)
    │  Rejects login walls, strips chrome, cleans data
    │  Returns: cleaned dict or None
    ▼
database.py: save_post(post_data, account)
    │  Computes content_hash
    │  Checks for edit (hash mismatch → save to post_versions)
    │  INSERT or UPDATE posts table
    │  Sets first_seen_at (INSERT only), last_seen_at (always)
    ▼
comments.py: extract_comments(page)
    │  Returns: list[Comment] (author, text, timestamp, is_reply, depth)
    ▼
sanitize.py: sanitize_comments(comments)
    │  Filters garbage, returns clean list
    ▼
database.py: save_comments(post_id, comments)
    │  Computes content_hash per comment
    │  Infers depth from is_reply if not set
    │  INSERT with ON CONFLICT → update last_seen_at
    ▼
downloader.py: download_attachments(...)
    │  Downloads images/videos to local disk
    ▼
database.py: save_attachments(post_id, attachments_dict)
    │  Records in attachments table
    ▼
database.py: queue_media_batch(post_id, images, videos)
    │  Adds undownloaded media to media_queue
```

### Path 2: Tampermonkey Script → `/api/ingest`

```
User's Browser (facebook.com)
    │
    ▼
fb-monitor-collector.user.js
    │  Extracts: posts with comments, images (canvas→base64), videos
    │  Computes: comment depth via DOM nesting
    ▼
POST http://localhost:8150/api/ingest
    │  Payload: { page_name, page_url, posts: [...] }
    ▼
web_ui.py: api_ingest()
    │  For each post:
    │    ├─ Check if post_id exists → skip if duplicate
    │    ├─ sanitize_post() → reject login walls
    │    ├─ db.save_post(post_data, account="extension")
    │    ├─ sanitize_comments() → filter garbage
    │    ├─ db.save_comments(post_id, comments)
    │    ├─ Save inline image_data (base64 → file)
    │    │   OR download image_urls from CDN
    │    ├─ db.save_attachments(post_id, ...)
    │    └─ db.queue_media_batch(post_id, [], video_urls)
    ▼
Response: { saved: N, skipped: N, comments: N, images_saved: N, videos_queued: N }
```

### Path 3: Import Queue → `/api/import`

```
User pastes raw text (URLs, console output, anything)
    │
    ▼
POST /api/import (or web form at /import/add)
    │
    ▼
web_ui.py: extract_fb_urls(raw_text)
    │  Regex: extracts /posts/, /permalink, /videos/, /reel/, /photos/, etc.
    │  Strips query params (except permalink.php, story.php)
    │  Deduplicates
    ▼
database.py: add_import_urls(url_list, page_name)
    │  INSERT into import_queue (status='pending')
    ▼
fb_monitor.py Phase 3: process_imports()
    │  Picks up pending URLs
    │  Opens each in Playwright → parse_post() → save
    │  Updates import_queue status to 'scraped' or 'failed'
```

---

## Data Flow: Storage → Web UI Display

### Post List (`/posts`)

```
web_ui.py: posts_list()
    │
    ├─ db.get_posts(page_name, search, category_id, entity_id, offset)
    │    SQL: SELECT * FROM posts WHERE ... ORDER BY detected_at DESC LIMIT 50
    │    Full-text search: WHERE text LIKE '%query%' OR post_id IN (
    │        SELECT post_id FROM comments WHERE text LIKE '%query%'
    │    )
    │
    ├─ For each post: enrich with counts
    │    SELECT COUNT(*) FROM attachments WHERE post_id=? AND type='image'
    │    SELECT COUNT(*) FROM attachments WHERE post_id=? AND type='video'
    │    SELECT COUNT(*) FROM comments WHERE post_id=?
    │    db.get_categories_for_post(post_id)
    │
    └─ Render: templates/posts.html
```

### Post Detail (`/posts/{post_id}`)

```
web_ui.py: post_detail()
    │
    ├─ db.get_post(post_id)          → post dict
    ├─ db.get_comments_for_post()    → list of comment dicts
    │    (includes: depth, is_deleted, first_seen_at, content_hash)
    ├─ db.get_attachments_for_post() → list of attachment dicts
    ├─ db.get_post_versions()        → list of version dicts (edit history)
    ├─ db.get_people_for_post()      → linked people with roles
    ├─ db.get_categories_for_post()  → assigned categories
    ├─ db.get_media_queue_for_post() → pending media downloads
    │
    └─ Render: templates/post_detail.html
         │
         ├─ Post header: author, timestamp, captured date
         │   If is_deleted: shows "DELETED" badge with timestamp
         │   Audit info: first_seen, last_seen, content_hash
         │
         ├─ Post text
         │
         ├─ Version history (if post_versions exist):
         │   Expandable "<details>" showing old text + hash + capture time
         │
         ├─ Attachments: images (full-width), videos (player or placeholder)
         │   Queued media: pending download buttons
         │
         ├─ Engagement bar: reactions, comments count (deleted count), shares
         │
         ├─ Comments section:
         │   Each comment rendered with:
         │   - Depth-based indentation (depth-0, depth-1, depth-2, depth-3)
         │   - Deleted badge + dashed border if is_deleted
         │   - Metadata: timestamp, first_seen_at, deleted_at
         │   - "Retrieve Comments" button (triggers Playwright fetch)
         │
         └─ Admin tools (collapsible):
             - Category tagging
             - Person linking (with roles)
```

### Page Feed (`/pages/{name}`)

```
web_ui.py: page_feed()
    │
    ├─ db.get_posts(page_name=name, limit=100)
    ├─ Filter by owner/community (author == page_name or not)
    ├─ Enrich: attachment counts, comment counts, first image
    ├─ db.get_people_for_page(page_name)
    ├─ db.get_entities_for_page(page_name)
    │
    └─ Render: templates/page_feed.html
         Tabs: All / Page Posts / Community Posts
```

### Dashboard (`/`)

```
web_ui.py: dashboard()
    │
    ├─ db.get_stats()
    │    Returns: {
    │      total_posts, total_comments, total_attachments,
    │      total_pages, total_people, total_entities,
    │      posts_today, posts_this_week,
    │      latest_post_date, total_import_pending
    │    }
    │
    └─ Render: templates/dashboard.html
```

---

## API Response Shapes

### `GET /api/posts/{post_id}`

```json
{
    "id": 42,
    "post_id": "pfbid0...",
    "page_name": "Vote Kevin Crye",
    "page_url": "https://www.facebook.com/votekevincrye",
    "post_url": "https://www.facebook.com/votekevincrye/posts/pfbid0...",
    "author": "Vote Kevin Crye",
    "text": "Full post text here...",
    "timestamp": "February 20, 2026 at 3:45 PM",
    "timestamp_raw": "February 20, 2026 at 3:45 PM",
    "shared_from": null,
    "shared_original_url": null,
    "links": "[\"https://example.com\"]",
    "reaction_count": "42",
    "comment_count_text": "10 comments",
    "share_count_text": "3 shares",
    "post_dir": "downloads/Vote_Kevin_Crye/20260220_154500_pfbid0/",
    "account": "anonymous",
    "detected_at": "2026-02-20T15:45:00+00:00",
    "content_hash": "a9f30e9d65fa8505",
    "first_seen_at": "2026-02-20T15:45:00+00:00",
    "last_seen_at": "2026-02-24T10:00:00+00:00",
    "is_deleted": 0,
    "deleted_at": null,
    "comments": [
        {
            "id": 101,
            "post_id": "pfbid0...",
            "author": "John Doe",
            "text": "Great post!",
            "timestamp": "2h",
            "is_reply": 0,
            "detected_at": "2026-02-20T16:00:00+00:00",
            "content_hash": "75e903848f13f650",
            "first_seen_at": "2026-02-20T16:00:00+00:00",
            "last_seen_at": "2026-02-24T10:00:00+00:00",
            "is_deleted": 0,
            "deleted_at": null,
            "parent_comment_id": null,
            "root_comment_id": null,
            "depth": 0
        },
        {
            "id": 102,
            "author": "Jane Smith",
            "text": "I agree!",
            "depth": 1,
            "is_reply": 1,
            "is_deleted": 0
        }
    ],
    "attachments": [
        {
            "id": 10,
            "post_id": "pfbid0...",
            "type": "image",
            "url": "https://scontent.../image.jpg",
            "local_path": "downloads/.../attachments/image_1.jpg",
            "filename": "image_1.jpg"
        }
    ],
    "people": [
        {"id": 1, "name": "Kevin Crye", "role": "author"}
    ],
    "versions": [
        {
            "id": 1,
            "post_id": "pfbid0...",
            "text": "Original text before edit",
            "content_hash": "b3c4d5e6f7a8b9c0",
            "captured_at": "2026-02-21T08:00:00+00:00"
        }
    ]
}
```

### `GET /api/posts/search?q=keyword&limit=50`

```json
[
    {
        "id": "pfbid0...",
        "text": "Post text\n\nJohn Doe: comment text\nJane Smith: reply text",
        "page_name": "Vote Kevin Crye",
        "author": "Vote Kevin Crye",
        "date": "2026-02-20T15:45:00+00:00",
        "post_url": "https://www.facebook.com/..."
    }
]
```

Note: search results inline all comments into the `text` field for full-text
matching. This is the format Atlas uses for RAG/search.

### `POST /api/ingest`

**Request:**
```json
{
    "page_name": "Vote Kevin Crye",
    "page_url": "https://www.facebook.com/votekevincrye",
    "posts": [
        {
            "post_id": "pfbid0...",
            "post_url": "https://...",
            "author": "Vote Kevin Crye",
            "text": "Post content...",
            "timestamp": "February 20, 2026",
            "image_urls": ["https://scontent.../..."],
            "image_data": [{"data": "base64...", "content_type": "image/jpeg"}],
            "video_urls": [],
            "reaction_count": "42",
            "comment_count_text": "10 comments",
            "share_count_text": "3 shares",
            "shared_from": null,
            "links": ["https://example.com"],
            "comments": [
                {
                    "author": "John Doe",
                    "text": "Great post!",
                    "timestamp": "2h",
                    "is_reply": false,
                    "depth": 0
                }
            ]
        }
    ]
}
```

**Response:**
```json
{
    "saved": 1,
    "skipped": 0,
    "comments": 1,
    "images_saved": 1,
    "videos_queued": 0,
    "total_submitted": 1
}
```

### `POST /api/import`

Accepts raw text (any format — URLs, console output, JSON, HTML).
Extracts Facebook URLs automatically.

**Response:**
```json
{
    "extracted": 5,
    "added": 3,
    "skipped": 2,
    "urls": ["https://facebook.com/..."]
}
```

---

## Content Hash Algorithm

```python
def content_hash(text: str, media_urls: list[str] = None) -> str:
    normalized = (text or "").strip()
    media_part = "|".join(sorted(media_urls or []))
    payload = f"{normalized}\n{media_part}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
```

- **Posts:** hash includes text + sorted image/video URLs
- **Comments:** hash includes text only (no media)
- **Stability:** same content always produces same hash
- **Edit detection:** hash mismatch triggers version archival

---

## Database Migration

The `init_db()` function handles both fresh databases and existing ones:

1. `CREATE TABLE IF NOT EXISTS` — creates all tables
2. `_migrate_add_columns()` — adds new columns to existing tables (idempotent)
3. Backfills `first_seen_at` / `last_seen_at` from `detected_at` where NULL

This means you can deploy code changes that add columns without dropping the
database. The migration runs on every startup.

---

## Config Reference (`config.json`)

```json
{
    "pages": [                              // Pages to monitor
        {"name": "Page Name", "url": "https://facebook.com/...", "enabled": true}
    ],
    "default_account": "anonymous",          // Default scraping account
    "feed_extraction": true,                 // Use feed-based extraction
    "feed_poll_minutes": 3,                  // Wait between page polls in feed mode
    "feed_max_retries": 5,                   // Max retries per page in feed mode
    "max_post_age_days": 7,                  // Ignore posts older than this
    "check_interval_minutes": 15,            // Main loop interval
    "comment_tracking_hours": 24,            // Active tracking window
    "comment_recheck_interval_minutes": 30,  // Recheck interval (active tier)
    "max_requests_per_hour": 30,             // Anonymous rate limit
    "logged_in_polling": {
        "max_requests_per_hour": 8,
        "delay_between_pages_sec": [15, 45],
        "delay_between_posts_sec": [10, 30],
        "comment_recheck_interval_minutes": 90
    },
    "output_dir": "downloads",               // Media download directory
    "skip_media_downloads": false,
    "auto_download_logged_in": false,        // Download media immediately for logged-in
    "download_proxy": {                      // Optional VPS proxy
        "url": "", "token": ""
    },
    "headless": true,                        // Run browsers headless
    "tor": {
        "enabled": true,
        "socks_port": 9050,
        "control_port": 9051,
        "pool_size": 10,
        "pool_base_socks_port": 9060
    },
    "notifications": {
        "enabled": false,
        "discord_webhook_url": "",
        "ntfy_topic": ""
    }
}
```

---

## State Management

### `state.json` (managed by `tracker.py`)

```json
{
    "seen_posts": {
        "Vote_Kevin_Crye": ["pfbid0abc...", "pfbid0def..."],
        "ShastaCountyGov": ["1234567890"]
    },
    "active_tracking": [
        {
            "post_id": "pfbid0abc...",
            "post_url": "https://facebook.com/...",
            "post_dir": "downloads/...",
            "page_name": "Vote Kevin Crye",
            "account": "anonymous",
            "detected_at": "2026-02-24T10:00:00+00:00",
            "last_comment_check": "2026-02-24T12:30:00+00:00",
            "comment_checks": 5
        }
    ]
}
```

### `extractor_health.json`

```json
{
    "aria_articles": {"last_success": "...", "consecutive_failures": 0},
    "data_pagelet": {"last_success": "...", "consecutive_failures": 3},
    "feed_unit": {"last_success": null, "consecutive_failures": 10}
}
```

### `scraper_status.json` (read by web UI)

```json
{
    "state": "scraping",
    "page_name": "Vote Kevin Crye",
    "current_page": 3,
    "total_pages": 24,
    "cycle": 42,
    "cycle_stats": {"posts_found": 5, "new_posts": 2, "images": 3, "videos": 0},
    "tor": {"healthy": 8, "total": 10, "login_walls": 2},
    "next_poll": "2026-02-24T10:15:00+00:00",
    "updated_at": "2026-02-24T10:00:30+00:00"
}
```
