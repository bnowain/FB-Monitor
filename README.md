# Facebook Page Monitor

Monitors public Facebook pages for new posts and captures structured data for accountability and transparency work.

## What It Captures

For each new post detected:

```
downloads/page_name/
  20250208_143022_pfbid02abc/
    post.json                       # Full post data
    comments.json                   # Comments, updated over 24h
    attachments/
      image_1.jpg                   # Downloaded images
      video_01.mp4                  # Downloaded videos
```

### post.json

```json
{
  "post_id": "pfbid02abc...",
  "url": "https://www.facebook.com/...",
  "page_name": "Shasta County Board of Supervisors",
  "author": "Shasta County Board of Supervisors",
  "text": "The full text of the post...",
  "timestamp": "February 8, 2025 at 3:45 PM",
  "shared_from": "City of Redding",
  "shared_original_url": "https://www.facebook.com/CityofRedding/posts/...",
  "links": ["https://example.com/agenda.pdf"],
  "attachments": {
    "images": ["attachments/image_1.jpg"],
    "videos": ["attachments/video_01.mp4"]
  },
  "detected_at": "2025-02-08T23:45:22+00:00"
}
```

### comments.json

Comments are rechecked periodically for 24 hours after detection.

```json
{
  "post_url": "https://www.facebook.com/...",
  "last_updated": "2025-02-09T23:45:22+00:00",
  "total_comments": 14,
  "comments": [
    {
      "author": "Jane Doe",
      "text": "What about the budget shortfall?",
      "timestamp": "2h",
      "is_reply": false
    }
  ]
}
```

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

Verify yt-dlp is available: `yt-dlp --version`

## Account Setup

Different pages may require different Facebook accounts (e.g. public pages vs private groups). Each account gets its own saved browser session.

### Setting up accounts

```bash
# Log in to an account (opens a browser window)
python fb_monitor.py --login primary

# Log in to additional accounts
python fb_monitor.py --login work
python fb_monitor.py --login personal

# See all saved sessions
python fb_monitor.py --accounts

# Remove a saved session
python fb_monitor.py --logout work
```

When you run `--login`, a browser window opens to facebook.com. Log in normally, then close the window. The session is saved to `profiles/<account_name>/` and reused on future runs.

### Mapping accounts to pages

In `config.json`, assign accounts to pages:

```json
{
  "default_account": "primary",
  "pages": [
    {
      "name": "Public Government Page",
      "url": "https://www.facebook.com/GovPage",
      "enabled": true
    },
    {
      "name": "Private Community Group",
      "url": "https://www.facebook.com/groups/CommunityGroup",
      "account": "work",
      "enabled": true
    },
    {
      "name": "Another Restricted Page",
      "url": "https://www.facebook.com/RestrictedPage",
      "account": "personal",
      "enabled": true
    }
  ]
}
```

**How account selection works:**

1. If the page has an `"account"` field, uses that account
2. Otherwise uses `"default_account"` from the top-level config
3. If neither is set, runs anonymously (no login, public pages only)

The monitor groups pages by account and opens a separate browser session for each, so credentials are never mixed.

## Configuration

Full `config.json` reference:

| Setting | Default | Description |
|---------|---------|-------------|
| `default_account` | `"anonymous"` | Account to use when page doesn't specify one |
| `check_interval_minutes` | `15` | Base interval between checks (±40% random jitter) |
| `comment_tracking_hours` | `24` | How long to keep rechecking comments on a post |
| `comment_recheck_interval_minutes` | `30` | How often to recheck comments on tracked posts |
| `max_requests_per_hour` | `30` | Rate limit — page loads per hour before throttling |
| `headless` | `true` | Set `false` to watch the browser (debugging) |

### Notifications

- **Discord**: Paste a webhook URL into `discord_webhook_url`
- **ntfy.sh**: Set a topic name, install the ntfy app for free push alerts

## Usage

```bash
# Run once — detect new posts + recheck tracked comments
python fb_monitor.py

# Watch mode — continuous monitoring with jittered timing
python fb_monitor.py --watch

# List configured pages and account assignments
python fb_monitor.py --list

# Manage accounts
python fb_monitor.py --login <name>     # set up a new account
python fb_monitor.py --accounts         # list saved sessions
python fb_monitor.py --logout <name>    # delete a session

# Diagnostics
python fb_monitor.py --status           # comment tracking status
python fb_monitor.py --health           # extractor strategy health

# Maintenance
python fb_monitor.py --reset            # clear all state
```

## Running on a Schedule

```bash
# Cron: every 15 minutes
*/15 * * * * cd /path/to/fb-monitor && python fb_monitor.py >> monitor.log 2>&1
```

Or use `--watch` for continuous operation with randomized intervals.

## Anti-Detection

- **Randomized timing** — check intervals vary ±40% (triangular distribution)
- **Human-like behavior** — variable scroll speeds, random pauses, occasional scroll-up
- **Rotating fingerprints** — randomized user agent, viewport, timezone each cycle
- **Stealth JavaScript** — masks `navigator.webdriver` and automation tells
- **Rate limiting** — configurable max requests/hr, auto-throttles near limit
- **Per-account sessions** — each account is a separate browser profile

## Architecture

| File | Purpose |
|------|---------|
| `fb_monitor.py` | Main entry point and orchestrator |
| `extractors.py` | Resilient post detection (5 strategies with fallback) |
| `post_parser.py` | Structured post data extraction |
| `comments.py` | Comment extraction with incremental merge |
| `downloader.py` | Image and video downloading |
| `stealth.py` | Anti-detection (jitter, fingerprints, rate limiting) |
| `sessions.py` | Multi-account browser session management |
| `tracker.py` | Persistent state (seen posts, comment tracking jobs) |
| `config.json` | Configuration |
| `requirements.txt` | Python dependencies |

Auto-generated at runtime:

| Path | Purpose |
|------|---------|
| `state.json` | Seen posts and active tracking jobs |
| `extractor_health.json` | Strategy health metrics |
| `profiles/` | Saved browser sessions per account |
| `downloads/` | All captured post data and attachments |
