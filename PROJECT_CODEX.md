# FB-Monitor — Project Codex

**Last updated:** 2026-02-24
**Platform focus:** Facebook only (multi-platform expansion planned but not active)

This document is the living reference for any agent or developer picking up this
project. Read it before making changes.

---

## What This Project Does

FB-Monitor is an automated public Facebook page archival system for civic
accountability journalism. It scrapes posts and comments from configured public
Facebook pages, downloads media, detects edits and deletions, and provides a
web UI + REST API for browsing and enriching the collected data.

**Use case:** Monitoring local government officials, police departments, news
outlets, and community pages in Shasta County, California for a civic
accountability research platform.

---

## How It Works — The Big Picture

```
┌── INGESTION ─────────────────────────────────────────────────────────┐
│                                                                       │
│  ┌─ fb_monitor.py (continuous) ──────────────────────────────────┐   │
│  │  Phase 1: Detect new posts on configured pages                │   │
│  │  Phase 2: Recheck comments on tracked posts (tiered schedule) │   │
│  │  Phase 3: Process import queue (bulk URL backfill)            │   │
│  └───────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ┌─ Tampermonkey script ─────────────────────────────────────────┐   │
│  │  User browses Facebook normally → script auto-expands posts    │   │
│  │  → extracts data → POSTs to /api/ingest                       │   │
│  └───────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ┌─ deep_scrape.py (one-off) ───────────────────────────────────┐   │
│  │  Backfill: scroll entire page history with logged-in account   │   │
│  └───────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────┘
          │                    │                    │
          ▼                    ▼                    ▼
    ┌─────────────────────────────────────────────────────┐
    │              database.py → fb_monitor.db             │
    │   posts, comments, attachments, people, entities,    │
    │   categories, post_versions, comment_versions,       │
    │   import_queue, media_queue                          │
    └─────────────────────────────────────────────────────┘
          │
          ▼
    ┌─────────────────────────────────────────────────────┐
    │              web_ui.py → localhost:8150               │
    │   HTML pages: dashboard, posts, pages, people,       │
    │   entities, categories, downloads, import            │
    │   REST API: /api/posts, /api/search, /api/people...  │
    └─────────────────────────────────────────────────────┘
```

---

## Three Ingestion Paths

### 1. Automated Scraper (`fb_monitor.py`)

The main loop runs continuously in three phases:

**Phase 1 — Detect new posts:**
- Iterates through each page in `config.json`
- Opens page feed in Playwright browser
- Injects `injected_collector.js` via `collector.py` for extraction
- Fallback: uses `extractors.py` (5 independent DOM strategies)
- For each new post: navigates to post URL, parses with `post_parser.py`
- Downloads attachments via `downloader.py`
- Saves to DB, marks as "seen" in `state.json`
- Registers post for comment tracking

**Phase 2 — Recheck comments:**
- Uses tiered scheduling (see Lookback Tiers below)
- Revisits tracked posts, extracts current comments
- Detects new comments, edits (via content_hash), deletions (tombstones)
- Updates `last_seen_at` for comments still visible
- Tombstones comments missing for 48+ hours

**Phase 3 — Process imports:**
- Processes URLs from the import queue (added via web UI or API)
- Same extraction pipeline as Phase 1

### 2. Tampermonkey Script (`fb-monitor-collector.user.js`)

User installs this in their browser. When browsing Facebook:
- Floating control panel appears (bottom-right)
- Auto-expands "See more", "View N comments", reply threads
- On button click: extracts all visible posts + comments + images
- POSTs to `http://localhost:8150/api/ingest`
- Server-side: sanitizes, computes content_hash, saves to DB
- Images captured directly from browser canvas (no extra network calls)

**Comment depth:** The script computes actual nesting depth (not just is_reply
boolean) by walking the `li > ul > li` DOM chain.

### 3. Deep Scrape (`deep_scrape.py`)

One-time historical backfill via `python fb_monitor.py --deep-scrape "Page Name"`:
- Uses a logged-in account (never Tor)
- Scrolls entire page history (up to 200 scrolls)
- Processes each post individually: full text, all comments, media
- Rate-limited with human-like delays (5-15s between posts)

---

## Anti-Detection System

Facebook actively blocks scrapers. This project has layered defenses:

### Tor Pool (`tor_pool.py`)
- Manages N parallel Tor instances (default pool_size: 10)
- Ports: SOCKS on 9060+, Control on 9160+
- **Race strategy:** tries multiple exit nodes in parallel, uses first one that
  doesn't hit a login wall
- **Bootstrap acceleration:** seeds pool instances with cached relay descriptors
  from the main Tor instance (30s vs 5+ min bootstrap)
- **Health monitor:** background thread every 10s; auto-restarts crashed/stalled
  instances
- **Login wall cooldown:** when an exit hits a login wall, instance gets 5-min
  cooldown + NEWNYM rotation

### Stealth (`stealth.py`)
- **Browser fingerprints:** 10 coherent profiles (UA + platform + vendor matched)
- **WebGL spoofing:** randomized renderer strings
- **Viewport rotation:** varies window size to avoid fingerprinting
- **Human-like scrolling:** variable speed, pauses, small jitter
- **Rate limiting:** per-account tracking (anonymous: 30/hr, logged-in: 8/hr)
- **Request jitter:** randomized delays between all actions

### Sessions (`sessions.py`)
- **Anonymous sessions:** use Tor SOCKS5 proxy, disposable
- **Logged-in sessions:** persistent browser profiles in `profiles/` directory
- Logged-in accounts never use Tor (would trigger Facebook's "new device" flow)
- Setup: `python fb_monitor.py --login <account_name>`

---

## Extraction Pipeline

### Post Extraction (`extractors.py`)

Five strategies, tried in order. Health tracking (`extractor_health.json`)
monitors which are working:

1. **ARIA articles** — `[role="article"]` containers
2. **Data-pagelet** — Facebook's server-rendered pagelet containers
3. **Feed unit divs** — `div[data-pagelet*="FeedUnit"]`
4. **Generic link scan** — scans for post URLs in all `<a>` tags
5. **JS injected collector** — injects `injected_collector.js` for full extraction

When all strategies fail → Facebook likely changed their DOM. Check
`extractor_health.json` for degradation patterns.

### Post Parsing (`post_parser.py`)

Three strategies for extracting structured data from individual post pages:

1. **ARIA roles** — structured data from accessibility attributes
2. **Data attributes** — Facebook's data-ad-rendering-role markers
3. **Text blocks** — fallback: largest text block heuristic

### Comment Extraction (`comments.py`)

Multi-strategy comment extraction with depth tracking:

1. **ARIA strategy** — `ul[role="list"]` nested comment trees. Computes depth
   by walking `li > ul > li` nesting chains.
2. **Mobile strategy** — loads `m.facebook.com` version for simpler DOM
3. **Injected strategy** — uses `injected_collector.js` comment extraction

**Depth tracking:** Comments have `depth` field (0 = top-level, 1 = reply,
2 = reply-to-reply, etc.). Used for threaded display in the web UI.

### Data Cleaning (`sanitize.py`)

All data passes through sanitization:
- **Login wall detection:** rejects entire pages that are login walls
- **Chrome stripping:** removes "Log in", "Sign up", "Forgot password?" etc.
- **Garbage filtering:** drops posts/comments that are just UI text
- **Reaction count cleaning:** normalizes "5.2K" → numeric
- **Timestamp resolution:** converts relative ("2h ago") to absolute datetime

---

## Content Integrity System (Phase 1 — Current)

### Content Hashing

Every post and comment gets a SHA-256 hash (truncated to 16 hex chars):
```python
content_hash(text, media_urls) → "a9f30e9d65fa8505"
```
- Posts: hash includes text + sorted media URLs
- Comments: hash includes text only

### Edit Detection

When `save_post()` is called and the content_hash differs from the stored value:
1. Old version is saved to `post_versions` table
2. Current version is updated with new hash
3. `last_seen_at` is updated

**Accessible via:**
- Web UI: post detail page shows expandable "N previous versions (post was edited)"
- API: `GET /api/posts/{id}` includes `versions` array

### Delete Detection (Tombstoning)

During comment rechecks:
1. Build set of content_hashes from freshly fetched comments
2. Match against existing DB comments
3. Update `last_seen_at` for matched comments
4. Comments not seen for 48+ hours → `is_deleted = 1, deleted_at = <now>`

**Conservative approach:** 48-hour threshold prevents false positives from
Facebook's comment loading inconsistencies (collapsed threads, "Most relevant"
filtering, network issues).

### Audit Trail

All posts and comments track:
- `first_seen_at` — when this content was first captured
- `last_seen_at` — when it was last confirmed visible
- `content_hash` — current hash for change detection
- `is_deleted` / `deleted_at` — tombstone flag and timestamp

### Lookback Scheduling Tiers (`tracker.py`)

Posts are rechecked at decreasing frequency as they age:

| Age | Interval | Tier Name |
|-----|----------|-----------|
| 0-24h | config `comment_recheck_interval_minutes` (default 30m) | active |
| 24-48h | every 6 hours | lookback_48h |
| 48h-7d | every 24 hours | lookback_7d |
| 7d-30d | every 7 days | lookback_30d |
| >30d | pruned from tracking | — |

---

## Collector Pipeline Detail

### Playwright → Injected JS → Python

```
collector.py                    injected_collector.js (in browser)
    │                                    │
    ├─ inject(page) ──────────────────► window.__fbm created
    │                                    │
    ├─ Phase 1: openCommentSections() ─► clicks "N comments" buttons
    ├─ Phase 2: switchToAllComments() ─► clicks filter → "All comments"
    ├─ Phase 3: expandThreads() ───────► clicks "See more", reply expanders
    │   (batched: 10 rounds per call)    (loops until no buttons remain)
    ├─ Phase 4: extractPosts() ────────► returns raw post dicts with comments
    ├─ Phase 5: captureImages() ───────► canvas → base64 (optional)
    │                                    │
    └─ _clean_posts() ◄────────────────┘
       (sanitize.py applied Python-side)
```

### Tampermonkey vs. Injected Collector

| Feature | Tampermonkey (`*.user.js`) | Injected (`injected_collector.js`) |
|---------|---------------------------|--------------------------------------|
| Runs in | User's browser | Playwright headless browser |
| Trigger | Auto on Facebook pages | Injected by `collector.py` |
| UI panel | Yes (floating widget) | No |
| API push | Yes (POSTs to /api/ingest) | No (data returned to Python) |
| MutationObserver | Yes (auto-expand on scroll) | No |
| GM_* APIs | Yes | No |
| Exposed API | Internal | `window.__fbm` object |

Both share the same extraction logic but are adapted for their contexts.

---

## Media Downloads (`downloader.py`)

Three download modes:

1. **Direct** — fetch from Facebook CDN (default)
2. **Tor SOCKS5** — route through Tor proxy
3. **Remote proxy** — route through VPS running `download_proxy_server.py`

### Download Proxy (`download_proxy_server.py`)

Deployed on a separate VPS. Protects home IP from Facebook CDN:

```bash
# On VPS:
python download_proxy_server.py --port 9100 --token SECRET

# In config.json:
"download_proxy": { "url": "http://vps:9100", "token": "SECRET" }
```

Endpoints:
- `GET /fetch?url=...` — stream image from CDN
- `GET /fetch-video?post_url=...` — download via yt-dlp, stream back
- Domain whitelist: only Facebook CDN domains allowed
- Rate limited: 1 req/sec minimum

---

## Web UI + API (`web_ui.py`)

### HTML Pages

| Route | Page | Key Features |
|-------|------|--------------|
| `/` | Dashboard | Stats overview, scraper status |
| `/pages` | Page list | All monitored pages with post counts |
| `/pages/{name}` | Page feed | Posts for one page, owner/community filter |
| `/posts` | All posts | Search, filter by page/category/entity |
| `/posts/{id}` | Post detail | Full text, comments (threaded), images, videos, version history, admin tools |
| `/people` | People list | Search, link counts |
| `/people/{id}` | Person detail | Linked pages, posts, comments, entities |
| `/entities` | Entity list | Organizations/groups |
| `/entities/{id}` | Entity detail | Linked pages, people, recent posts |
| `/categories` | Categories | Tag management |
| `/downloads` | Media queue | Pending/downloaded/skipped media |
| `/import` | URL import | Bulk paste Facebook URLs |

### REST API (for Atlas integration)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | Health check |
| GET | `/api/scraper-status` | Live scraper state |
| GET | `/api/posts/search?q=&limit=` | Full-text search posts+comments |
| GET | `/api/posts?page_name=&search=` | List/filter posts |
| GET | `/api/posts/{post_id}` | Post detail with comments, attachments, people, versions |
| GET | `/api/people?search=` | Search people |
| GET | `/api/people/{person_id}` | Person with linked pages/posts/comments |
| GET | `/api/entities?search=` | List entities |
| GET | `/api/entities/{entity_id}` | Entity with linked pages/people |
| GET | `/api/stats` | Dashboard statistics |
| GET | `/api/categories` | List categories |
| POST | `/api/import` | Bulk URL import (accepts raw text, extracts FB URLs) |
| POST | `/api/ingest` | Browser extension data ingestion |
| POST | `/api/cleanup` | Run data quality cleanup |

### Scraper Status Broadcasting

`scraper_status.py` writes to `scraper_status.json`:
- What the scraper is currently doing (scraping page X of Y, rechecking comments, waiting)
- Cycle stats (posts found, new posts, images, videos)
- Tor pool health (healthy/total instances, login walls hit)
- Staleness detection: UI marks scraper as "offline" if no update for 300s

Read by web UI via `GET /api/scraper-status`.

---

## File Map

| File | Lines | Purpose |
|------|-------|---------|
| `fb_monitor.py` | 1804 | Main scraper — 3-phase cycle, CLI entry point |
| `database.py` | 1759 | SQLite schema (16 tables), all CRUD, edit/delete detection |
| `web_ui.py` | 1354 | FastAPI server — HTML UI + REST API |
| `tor_pool.py` | 969 | Parallel Tor instance management with racing |
| `fb-monitor-collector.user.js` | 730 | Tampermonkey script — browser-based collection |
| `stealth.py` | 671 | Anti-detection: fingerprints, jitter, rate limiting |
| `injected_collector.js` | 561 | Playwright-injectable version of collector |
| `downloader.py` | 542 | Image/video download (direct, Tor, proxy) |
| `sanitize.py` | 519 | Login wall detection, garbage filtering, cleaning |
| `post_parser.py` | 421 | 3-strategy post data extraction |
| `comments.py` | 402 | Multi-strategy comment extraction with depth |
| `deep_scrape.py` | 339 | One-time page history backfill |
| `extractors.py` | 338 | 5-strategy post detection with health tracking |
| `test_extract.py` | 293 | Manual extraction tester (not automated tests) |
| `collector.py` | 213 | Playwright ↔ injected JS orchestrator |
| `download_proxy_server.py` | 208 | VPS media proxy (separate deployment) |
| `sessions.py` | 207 | Account management, persistent browser profiles |
| `tracker.py` | 178 | State management, lookback scheduling tiers |
| `scraper_status.py` | 170 | Live status broadcasting to web UI |

### Config & State Files

| File | Purpose |
|------|---------|
| `config.json` | Page list, rate limits, proxy settings, Tor config |
| `state.json` | Seen posts, active tracking jobs (written by scraper) |
| `extractor_health.json` | Which extraction strategies are working |
| `scraper_status.json` | Current scraper activity (read by web UI) |
| `fb_monitor.db` | SQLite database (WAL mode) |
| `profiles/` | Persistent browser profiles for logged-in accounts |
| `downloads/` | Downloaded media organized by page/post |

---

## Atlas Integration

This project is a **spoke** in the Atlas hub-and-spoke architecture.

- **Spoke key:** `facebook_monitor`
- **Port:** 8150
- **Atlas tools:** `search_monitored_posts`, `get_monitored_post`,
  `search_monitored_people`, `list_monitored_pages`, `get_fb_monitor_entities`

Atlas connects via HTTP to the REST API endpoints. This app must remain
independently functional without Atlas.

---

## Running

```bash
# Start the web UI + API
python web_ui.py                    # localhost:8150

# Start the scraper (separate terminal)
python fb_monitor.py

# Deep scrape a page's full history
python fb_monitor.py --deep-scrape "Page Name"

# Login setup for an account
python fb_monitor.py --login myaccount

# List accounts
python fb_monitor.py --accounts
```

**Dependencies:** Python 3.10+, Playwright (chromium), FastAPI, uvicorn,
requests, yt-dlp (for video). Tor optional but recommended.

**No Redis required.** All state is SQLite + JSON files.

---

## Common Failure Modes

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| All extraction strategies return 0 posts | Facebook changed DOM | Check `extractor_health.json`, update selectors |
| Login walls on every request | Tor exit IPs blocked | Tor pool will auto-rotate; check pool health |
| Comments not loading | "Most relevant" filter hiding them | Script switches to "All comments" in Phase 2 |
| Images fail to download | CDN URLs expired | Use download proxy or queue for retry |
| Scraper shows "offline" in UI | Scraper process crashed | Restart `fb_monitor.py` |
| Database locked errors | Multiple writers | WAL mode should handle this; check for hung processes |

---

## Development Rules

1. **Anti-detection is critical.** Every network request must go through the
   stealth layer. Never add direct Facebook requests without rate limiting.
2. **This app must work standalone.** No dependencies on Atlas or other spokes.
3. **Facebook-only for now.** Multi-platform expansion is researched (see
   `SOCIAL_MEDIA_ARCHIVAL_PIPELINE.md`) but not being implemented yet.
4. **Content integrity matters.** Always compute content_hash. Always update
   first_seen_at/last_seen_at. The edit/delete detection system depends on it.
5. **No formal test suite yet.** See `CLAUDE.md` for test guidance. The
   `test_extract.py` file is a manual tester, not automated tests.
