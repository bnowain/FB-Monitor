# Facebook Page Monitor — Project Briefing

## Overview

This is a Python-based tool for monitoring public Facebook pages, capturing structured post data, downloading attachments, and tracking comments over time. It was built for civic accountability and transparency journalism, specifically monitoring local government Facebook pages in Shasta County, California.

The tool is part of a broader civic accountability ecosystem that includes meeting transcription (civic_media), news aggregation (article-tracker), campaign finance monitoring (netfile-tracker), and public records requests (Shasta-PRA-Backup).

## Architecture

The project is modular with each file handling a single responsibility:

```
fb-monitor/
├── fb_monitor.py      # Main orchestrator and CLI entry point
├── extractors.py      # Post detection from page feeds (5 fallback strategies)
├── post_parser.py     # Structured data extraction from individual posts
├── comments.py        # Comment extraction with incremental merge/update
├── downloader.py      # Image and video attachment downloading
├── stealth.py         # Anti-detection (jitter, fingerprints, rate limiting)
├── sessions.py        # Multi-account browser session management
├── tracker.py         # Persistent state (seen posts, comment tracking jobs)
├── config.json        # User configuration
├── requirements.txt   # Python dependencies
└── README.md          # User-facing documentation
```

### Data Flow

1. **`fb_monitor.py`** groups enabled pages by their assigned Facebook account
2. For each account, it opens a browser session via **`sessions.py`** (persistent profile for logged-in accounts, fresh context for anonymous)
3. **`extractors.py`** scans each page feed to find post URLs using 5 strategies that fall back to each other
4. For each new post, **`post_parser.py`** navigates to it and extracts structured data (text, author, timestamp, shared source, links, attachment URLs)
5. **`downloader.py`** downloads images (via requests) and videos (via yt-dlp)
6. **`comments.py`** captures initial comments, then the post is registered with **`tracker.py`** for 24-hour comment monitoring
7. On subsequent runs, **`tracker.py`** identifies posts due for comment rechecks, and **`comments.py`** merges new comments into the existing file without duplicates
8. All timing uses **`stealth.py`** for randomized intervals, human-like delays, rotating browser fingerprints, and rate limiting

### Output Structure

Each detected post creates a self-contained directory:

```
downloads/<page_name>/
  <timestamp>_<post_id>/
    post.json           # Structured post data
    comments.json       # Comments (updated over 24h)
    attachments/
      image_1.jpg
      video_01.mp4
```

### State Files (auto-generated)

- `state.json` — Tracks which posts have been seen per page, and active comment tracking jobs with timing metadata
- `extractor_health.json` — Tracks success/failure rates of each extraction strategy to detect DOM changes
- `profiles/<account>/` — Persistent Chromium browser profiles with saved Facebook sessions

## Key Design Decisions

### Why 5 extraction strategies with fallback

Facebook changes its DOM frequently. Rather than depending on one CSS selector that could break overnight, the extractor runs 5 independent strategies in priority order:

1. **ARIA articles** — `role="article"` semantic containers (most reliable when present)
2. **Timestamp anchors** — Finds relative time links ("2h", "Yesterday") that point to post URLs
3. **Link sweep** — Brute-force scans all `<a>` tags for URLs matching post patterns
4. **Mobile site** — Loads `m.facebook.com` which has a completely different, simpler DOM
5. **Raw HTML regex** — Regexes the raw page source for post URLs (last resort)

Results are deduplicated by post ID, with higher-priority strategies winning. Health tracking logs consecutive failures per strategy and warns when degradation is detected.

### Why persistent browser profiles for accounts

Different pages may require different Facebook accounts (public pages vs restricted groups vs private community pages). Each account gets its own Chromium user data directory under `profiles/`. The user logs in once via `--login <name>`, and the session persists across runs. Pages in config.json map to accounts via the `"account"` field, with `"default_account"` as fallback.

### Why 24-hour comment tracking

Comments on government Facebook posts often arrive over hours or days. Rather than capturing comments once and missing late responses, the tracker registers each new post for ongoing monitoring. Every `comment_recheck_interval_minutes` (default 30), tracked posts are revisited and new comments are merged into the existing `comments.json` using author+text deduplication. After `comment_tracking_hours` (default 24), the post is retired.

### Why jittered timing and anti-detection

Running automated requests on a fixed schedule creates a detectable pattern. The stealth module provides:

- **Jittered intervals** — Triangular distribution around the base interval (±40%), so check times are unpredictable
- **Human-like delays** — Log-normal distribution for page load waits, variable scroll speeds, occasional scroll-up
- **Rotating fingerprints** — Each cycle randomizes user agent, viewport size, timezone, locale
- **Stealth JavaScript** — Masks `navigator.webdriver`, spoofs plugins/languages
- **Rate limiter** — Tracks page loads per hour, auto-throttles at 80% capacity, blocks at limit

### Why structured JSON over screenshots

Screenshots are not searchable, not diffable, and can't be correlated with other data sources. The structured JSON output enables:

- Full-text search across archived posts
- Correlation with meeting transcripts, campaign finance data, and news coverage
- Tracking shared content chains (who shared what from where)
- Programmatic analysis of posting patterns

## Dependencies

- `playwright` — Browser automation (Chromium)
- `yt-dlp` — Facebook video downloading
- `requests` — Direct image downloads

Setup: `pip install -r requirements.txt && playwright install chromium`

## CLI Reference

```
python fb_monitor.py                    # Run one cycle
python fb_monitor.py --watch            # Continuous monitoring
python fb_monitor.py --list             # Show pages + account assignments
python fb_monitor.py --login <name>     # Set up account (opens browser)
python fb_monitor.py --accounts         # List saved sessions
python fb_monitor.py --logout <name>    # Delete saved session
python fb_monitor.py --status           # Comment tracking status
python fb_monitor.py --health           # Extractor strategy health
python fb_monitor.py --reset            # Clear all state
python fb_monitor.py --config <path>    # Use alternate config file
```

## Known Limitations / Future Work

- **No login detection** — If a saved session expires, the tool doesn't detect the login wall and may silently capture less data. Could add session health checks.
- **No database backend** — Currently uses flat JSON files. For large-scale use, migrating to SQLite (consistent with the broader ecosystem) would enable search and cross-referencing.
- **Comment threading** — Reply detection is basic (checks DOM nesting). Proper parent-child threading would improve comment analysis.
- **No deduplication across accounts** — If two accounts monitor overlapping pages, the same post could be captured twice. Could add global post ID dedup.
- **Post edits** — Currently captures a post once. Doesn't detect or track edits to post text. Could hash post content and flag changes on comment rechecks.
- **Image OCR** — Downloaded images aren't analyzed. Could add OCR to extract text from image posts (common with government announcements).

## Integration Points

This tool is designed to eventually connect to the broader civic accountability ecosystem via the Atlas hub (port 8800). Potential integrations:

- **civic_media** — Cross-reference Facebook posts with meeting transcripts (e.g., "Supervisor X posted about this topic 2 hours after the board meeting")
- **article-tracker** — Correlate news coverage with government Facebook activity
- **netfile-tracker** — Link campaign donors mentioned in Facebook posts to contribution records
- **Shasta-PRA-Backup** — Connect public records requests to the Facebook discussions that prompted them

The structured JSON output with string IDs and timestamps is designed to be compatible with the ecosystem's SQLite/UUID conventions.

## Development Patterns

- **Incremental development** — Test frequently, commit often, use git as rollback mechanism
- **Maximum accuracy over speed** — Better to miss a post temporarily than capture garbage data
- **Additive-only state** — `seen_posts` list only grows; comment merges never remove existing comments
- **Fail gracefully** — Every extraction strategy is wrapped in try/except; failures in one strategy don't block others
- **Local-first** — No cloud dependencies, all data on disk, self-contained
