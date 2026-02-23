# Facebook Monitor — Public Facebook Page Monitoring

## Purpose

Automated monitoring and archival of public Facebook pages. Scrapes posts,
tracks comments over 24-hour windows, downloads media, and provides a web UI
plus REST API for browsing and enriching collected data.

## Architecture

- **Scraper:** `fb_monitor.py` — Playwright-based, multi-account (anonymous + logged-in)
- **Web UI + API:** `web_ui.py` — FastAPI on port 8150
- **Database:** SQLite at `fb_monitor.db` (WAL mode, 14 tables)
- **State:** `state.json` (seen posts, active tracking jobs)
- **Sessions:** `profiles/` directory (persistent browser profiles for logged-in accounts)
- **Config:** `config.json` (page list, rate limits, proxy settings)

## Key Components

| File | Purpose |
|------|---------|
| `fb_monitor.py` | Main scraper — 3-phase cycle (detect posts, recheck comments, process imports) |
| `web_ui.py` | FastAPI server — HTML UI + REST API (port 8150) |
| `database.py` | SQLite schema (14 tables) + all CRUD operations |
| `extractors.py` | 5-strategy post extraction with health tracking |
| `post_parser.py` | 3-strategy post data parser |
| `comments.py` | Multi-strategy comment extraction |
| `sanitize.py` | Login wall detection, garbage filtering, data cleaning |
| `sessions.py` | Account management — anonymous (Tor) + persistent profiles |
| `stealth.py` | Anti-detection — jitter, delays, user agent rotation, rate limiting |
| `downloader.py` | Image/video download (direct, proxy, yt-dlp) |
| `tracker.py` | JSON state management (seen posts, active jobs) |
| `config.json` | Page list, rate limits, proxy config, notifications |
| `download_proxy_server.py` | External VPS proxy for CDN bypass (separate deployment) |

## Database Tables (14)

posts, comments, attachments, people, people_pages, people_posts, people_comments,
categories, post_categories, entities, entity_pages, entity_people,
import_queue, media_queue

## REST API Endpoints (for Atlas)

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/health | Health check |
| GET | /api/posts/search?q=&limit= | Full-text search posts+comments |
| GET | /api/posts?page_name=&search= | List/filter posts |
| GET | /api/posts/{post_id} | Post detail with comments/attachments |
| GET | /api/people?search= | Search people |
| GET | /api/people/{person_id} | Person detail |
| GET | /api/entities | List entities |
| GET | /api/entities/{entity_id} | Entity detail |
| GET | /api/stats | Dashboard statistics |
| GET | /api/categories | List categories |
| POST | /api/import | Bulk URL import |
| POST | /api/ingest | Browser extension data ingestion |

## Atlas Integration

This project is a **spoke** in the Atlas hub-and-spoke architecture.
Atlas connects via HTTP to the REST API endpoints above.

- **Spoke key in Atlas:** `facebook_monitor`
- **Atlas config port:** 8150
- **Tools in Atlas:** search_monitored_posts, get_monitored_post,
  search_monitored_people, list_monitored_pages, get_fb_monitor_entities

**Cross-spoke rules:**
- This app must remain independently functional without Atlas or any other spoke.
- No direct spoke-to-spoke dependencies. All cross-app communication goes through Atlas.
  **Approved exceptions** (documented peer service calls):
  - `Shasta-PRA-Backup → civic_media POST /api/transcribe` — Transcription-as-a-Service
  New cross-spoke calls must be approved and added to this exception list.

## Development Notes

- Anti-detection is critical — Facebook actively blocks scrapers
- Rate limiting is per-account (anonymous: 30/hr, logged-in: 8/hr)
- Anonymous sessions use Tor SOCKS5 proxy if enabled
- Logged-in sessions use persistent browser profiles (never Tor)
- `extractor_health.json` tracks which extraction strategies are working
- When all strategies fail, Facebook likely changed their DOM

## Running

```bash
# Start the web UI + API
python -m web_ui  # or: python web_ui.py

# Start the scraper (separate terminal)
python fb_monitor.py

# Redis is NOT required (unlike civic_media)
```

## Ecosystem

This project is part of a hub-and-spoke civic accountability research platform.

**Hub:** Atlas (port 8888) — central orchestration, chat, search, RAG

**Spoke projects:**
- **civic_media** — meeting transcription, diarization, voiceprint learning
- **article-tracker** — local news aggregation and monitoring
- **Shasta-DB** — civic media archive browser and metadata editor
- **Facebook-Offline** — personal Facebook archive (private, local only)
- **Shasta-PRA-Backup** — public records requests browser
- **Shasta-Campaign-Finance** — campaign finance disclosures from NetFile
- **Facebook-Monitor** — public Facebook page monitoring (this project)

## Master Schema Reference

**`E:\0-Automated-Apps\MASTER_SCHEMA.md`** contains the canonical cross-project
database schema. If you add, remove, or modify any database tables or fields in
this project, **you must update the Master Schema** to keep it in sync. The agent
is authorized and encouraged to edit that file directly.

**`E:\0-Automated-Apps\MASTER_PROJECT.md`** describes the overall ecosystem
architecture and how all projects interconnect.
