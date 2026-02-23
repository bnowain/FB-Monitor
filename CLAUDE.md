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

## Testing

No formal test suite exists yet. Use Playwright for browser-based UI testing and pytest for API/service tests. `test_extract.py` in project root is a manual extraction tester, not part of the automated suite.

### Setup

```bash
pip install pytest httpx
# Playwright is already a production dependency (used by scraper)
python -m playwright install chromium  # if not already installed
```

### Running Tests

```bash
pytest tests/ -v
pytest tests/ -v -k "browser"    # Playwright UI tests only
pytest tests/ -v -k "api"        # API tests only
```

### Writing Tests

- **Browser tests** go in `tests/test_browser.py` — use Playwright to verify the web UI (post list, post detail with comments, people/entity pages, import flow, stats dashboard)
- **API tests** go in `tests/test_api.py` — use httpx against FastAPI endpoints (`/api/posts`, `/api/people`, `/api/search`)
- **Service tests** go in `tests/test_services.py` — unit tests for extractors, post parser, comment extraction, sanitization
- Playwright is already installed (primary scraper dependency)
- The server must be running at localhost:8150 for browser tests
- Do NOT run scraper tests against live Facebook — use saved HTML fixtures

### Key Flows to Test

1. **Post browsing**: list loads with filters, detail shows comments and attachments
2. **Search**: full-text search returns relevant posts and comments
3. **People/entities**: people and entity pages render with linked content
4. **Import**: bulk URL import via API creates import queue entries
5. **Stats**: dashboard shows correct aggregate statistics

## Master Schema & Codex References

**`E:\0-Automated-Apps\MASTER_SCHEMA.md`** — Canonical cross-project database
schema and API contracts. **HARD RULE: If you add, remove, or modify any database
tables, columns, API endpoints, or response shapes, you MUST update the Master
Schema before finishing your task.** Do not skip this — other projects read it to
understand this project's data contracts.

**`E:\0-Automated-Apps\MASTER_PROJECT.md`** describes the overall ecosystem
architecture and how all projects interconnect.

> **HARD RULE — READ AND UPDATE THE CODEX**
>
> **`E:\0-Automated-Apps\master_codex.md`** is the living interoperability codex.
> 1. **READ it** at the start of any session that touches APIs, schemas, tools,
>    chunking, person models, search, or integration with other projects.
> 2. **UPDATE it** before finishing any task that changes cross-project behavior.
>    This includes: new/changed API endpoints, database schema changes, new tools
>    or tool modifications in Atlas, chunking strategy changes, person model changes,
>    new cross-spoke dependencies, or completing items from a project's outstanding work list.
> 3. **DO NOT skip this.** The codex is how projects stay in sync. If you change
>    something that another project depends on and don't update the codex, the next
>    agent working on that project will build on stale assumptions and break things.
