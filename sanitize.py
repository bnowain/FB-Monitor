"""
sanitize.py — Data quality validation and cleaning for FB-Monitor.

Centralizes all cleaning logic: login wall detection, page chrome stripping,
garbage comment filtering, reaction count cleaning, and relative timestamp
resolution. Called from post_parser.py, web_ui.py, and database.py.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Login wall detection
# ---------------------------------------------------------------------------

_LOGIN_WALL_SIGNATURES = [
    "Log into Facebook",
    "Log in to Facebook",
    "Explore the things you love",
    "Email or mobile number\nPassword",
    "Email address or phone number\nPassword",
    "Create new account",
    "Sign Up\nIt's quick and easy",
    "Connect with friends and the world",
    "Facebook helps you connect",
    "Sign up to see photos",
    "You must log in to continue",
    "This content isn't available right now",
    "Go to News Feed",
]


def is_login_wall(text: str) -> bool:
    """Returns True if text looks like a Facebook login wall page."""
    if not text:
        return False
    hits = sum(1 for sig in _LOGIN_WALL_SIGNATURES if sig.lower() in text.lower())
    return hits >= 2


# ---------------------------------------------------------------------------
# Page chrome stripping
# ---------------------------------------------------------------------------

# Lines that are just page UI chrome (matched case-insensitively)
_CHROME_EXACT = {
    "log in",
    "forgot account?",
    "forgot password?",
    "sign up",
    "create new account",
    "not now",
    "see more",
    "email or mobile number",
    "password",
    "accessible login button",
}

# Patterns for chrome lines (leading)
_CHROME_PATTERNS = [
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$"),              # date lines like 2/14/2026
    re.compile(r"^[A-Z][a-z]+day,?\s+[A-Z][a-z]+ \d+"),    # "Monday, February 14"
    re.compile(r"^[A-Z][a-z]+ \d{1,2},?\s*\d{4}"),          # "February 14, 2026"
    re.compile(r"^[A-Z][a-z]+ \d{1,2}\s+at\s+\d"),          # "February 14 at 3:45 PM"
    re.compile(r"^[A-Z][a-z]+ \d{1,2}$"),                   # "January 22" (bare date)
    re.compile(r"^·$"),                                       # unicode dot separator
    re.compile(r"^[\s·•\u200b\u00a0\u202f]+$"),              # whitespace/dot-only lines (incl. narrow no-break space)
    re.compile(r"^\d+[hmdws]$", re.IGNORECASE),              # relative timestamps "4h", "6d"
    re.compile(r"^\d+\s*(hr|min|sec|hour|minute|day|week)s?\s*(ago)?$", re.IGNORECASE),
    re.compile(r"^(yesterday|today|just now)$", re.IGNORECASE),
    re.compile(r"^Favorites\s*·", re.IGNORECASE),            # "Favorites · February 19..."
]

# Trailing reel/video chrome pattern.
# Can appear as newline-separated or pipe-separated:
#   "| Vote Kevin Crye | Public | 22 | Reels"
#   "Vote Kevin Crye\nPublic\n22\nReels"
_REEL_CHROME_RE = re.compile(
    r"[\n|]\s*[A-Z][\w\s]+[\n|]\s*Public\s*(?:[\n|]\s*\d+\s*)*[\n|]\s*Reels\s*$",
    re.IGNORECASE,
)


def _normalize_whitespace(text: str) -> str:
    """Replace non-breaking spaces and narrow no-break spaces with regular spaces."""
    return text.replace("\u00a0", " ").replace("\u202f", " ").replace("\u200b", "")


def strip_page_chrome(text: str, page_name: str = "") -> str:
    """Strip leading lines of page chrome and trailing reel chrome from post text."""
    if not text:
        return text

    # Normalize unicode whitespace for matching
    text = _normalize_whitespace(text)

    lines = text.split("\n")
    page_lower = page_name.lower().strip() if page_name else ""

    # Walk from the top, stripping chrome lines
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        lower = stripped.lower()

        if not stripped:
            start = i + 1
            continue

        # Exact chrome matches
        if lower in _CHROME_EXACT:
            start = i + 1
            continue

        # Page name line
        if page_lower and lower == page_lower:
            start = i + 1
            continue

        # Pattern matches
        if any(p.match(stripped) for p in _CHROME_PATTERNS):
            start = i + 1
            continue

        # Hit real content — stop stripping
        break

    result = "\n".join(lines[start:]).strip()

    # Strip trailing reel/video chrome
    result = _REEL_CHROME_RE.sub("", result).strip()

    return result


# ---------------------------------------------------------------------------
# Reaction count cleaning
# ---------------------------------------------------------------------------

def clean_reaction_count(value: str) -> str:
    """Returns '' if value doesn't start with a digit (rejects 'See who reacted to this')."""
    if not value:
        return value
    value = value.strip()
    if value and value[0].isdigit():
        return value
    return ""


# ---------------------------------------------------------------------------
# Relative timestamp resolution
# ---------------------------------------------------------------------------

_RELATIVE_TS_RE = re.compile(
    r"^(\d+)\s*(s|m|h|d|w|hr|hrs|min|mins|sec|secs|hour|hours|minute|minutes|day|days|week|weeks)s?\s*(ago)?$",
    re.IGNORECASE,
)

_UNIT_MAP = {
    "s": "seconds", "sec": "seconds", "secs": "seconds",
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
    "w": "weeks", "week": "weeks", "weeks": "weeks",
}


def resolve_relative_timestamp(raw: str, reference_date: Optional[datetime] = None) -> str:
    """
    Convert relative timestamps like '6d', '3h', '1w' to ISO datetime.

    Uses reference_date as the base (defaults to now UTC).
    Returns the original string unchanged if it can't be parsed as relative.
    """
    if not raw:
        return raw

    raw_stripped = raw.strip()
    match = _RELATIVE_TS_RE.match(raw_stripped)
    if not match:
        return raw

    amount = int(match.group(1))
    unit_raw = match.group(2).lower()
    unit = _UNIT_MAP.get(unit_raw)
    if not unit:
        return raw

    if reference_date is None:
        reference_date = datetime.now(timezone.utc)
    elif reference_date.tzinfo is None:
        reference_date = reference_date.replace(tzinfo=timezone.utc)

    delta = timedelta(**{unit: amount})
    resolved = reference_date - delta
    return resolved.isoformat()


# ---------------------------------------------------------------------------
# Absolute timestamp parsing
# ---------------------------------------------------------------------------

# "February 6 at 6:00 PM", "February 9 at 11:42 AM"
_US_TS_RE = re.compile(
    r"^(\w+)\s+(\d{1,2})\s+at\s+(\d{1,2}):(\d{2})\s*(AM|PM)$",
    re.IGNORECASE,
)

# "14 February at 08:55" (UK/intl — 24-hour clock)
_UK_TS_RE = re.compile(
    r"^(\d{1,2})\s+(\w+)\s+at\s+(\d{1,2}):(\d{2})$",
    re.IGNORECASE,
)

# "January 23", "7 January" (date only, no time)
_DATE_ONLY_RE = re.compile(
    r"^(\w+)\s+(\d{1,2})$|^(\d{1,2})\s+(\w+)$",
    re.IGNORECASE,
)

_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_fb_timestamp(raw: str) -> Optional[datetime]:
    """
    Parse Facebook's absolute timestamp formats into a datetime.

    Handles:
      - ISO format: 2026-02-22T23:40:20.344155+00:00
      - US format:  February 6 at 6:00 PM
      - UK format:  14 February at 08:55
      - Date only:  January 23, 7 January

    Returns None if the format is not recognized.
    Assumes current year for formats without a year.
    """
    if not raw:
        return None

    raw = raw.strip().replace("\u202f", " ")

    # Already ISO format
    if raw.startswith("20"):
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass

    now = datetime.now(timezone.utc)

    # US: "February 6 at 6:00 PM"
    m = _US_TS_RE.match(raw)
    if m:
        month_name, day, hour, minute, ampm = m.groups()
        month = _MONTH_MAP.get(month_name.lower())
        if month:
            h = int(hour)
            if ampm.upper() == "PM" and h != 12:
                h += 12
            elif ampm.upper() == "AM" and h == 12:
                h = 0
            year = now.year
            dt = datetime(year, month, int(day), h, int(minute), tzinfo=timezone.utc)
            # If parsed date is in the future, it's from last year
            if dt > now + timedelta(days=1):
                dt = dt.replace(year=year - 1)
            return dt

    # UK: "14 February at 08:55"
    m = _UK_TS_RE.match(raw)
    if m:
        day, month_name, hour, minute = m.groups()
        month = _MONTH_MAP.get(month_name.lower())
        if month:
            year = now.year
            dt = datetime(year, month, int(day), int(hour), int(minute), tzinfo=timezone.utc)
            if dt > now + timedelta(days=1):
                dt = dt.replace(year=year - 1)
            return dt

    # Date only: "January 23" or "7 January"
    m = _DATE_ONLY_RE.match(raw)
    if m:
        if m.group(1) and m.group(2):
            # "January 23"
            month_name, day = m.group(1), m.group(2)
        else:
            # "7 January"
            day, month_name = m.group(3), m.group(4)
        month = _MONTH_MAP.get(month_name.lower())
        if month:
            year = now.year
            dt = datetime(year, month, int(day), 12, 0, tzinfo=timezone.utc)
            if dt > now + timedelta(days=1):
                dt = dt.replace(year=year - 1)
            return dt

    return None


def get_post_age_days(timestamp: str) -> Optional[float]:
    """
    Return the age of a post in days based on its timestamp.
    Returns None if the timestamp cannot be parsed.
    """
    dt = parse_fb_timestamp(timestamp)
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400


# ---------------------------------------------------------------------------
# Garbage comment detection
# ---------------------------------------------------------------------------

_GARBAGE_EXACT = {
    # Login/signup prompts
    "log in", "log into facebook", "log in to facebook",
    "forgot account?", "forgot password?",
    "create new account", "create an account",
    "sign up", "not now",
    # Page UI elements
    "like", "reply", "share", "comment",
    "write a comment", "write a comment…",
    "most relevant", "newest", "all comments",
    "see more", "see all",
    "no comments yet", "no comments yet.",
    "be the first to comment", "be the first to comment.",
    # Footer / legal
    "privacy", "privacy policy", "terms", "terms of service",
    "cookie policy", "cookies", "ad choices",
    "about", "help", "contact", "careers",
    "meta", "meta platforms, inc.",
    # Language names that leak from the locale picker
    "english (us)", "english (uk)", "español", "français",
    "deutsch", "português (brasil)", "italiano",
    "中文(简体)", "日本語", "한국어", "العربية", "हिन्दी",
}

_GARBAGE_PATTERNS = [
    re.compile(r"^\d+[hmdws]$", re.IGNORECASE),           # timestamps: "6d", "3h"
    re.compile(r"^\d+\s*(hr|min|sec|hour|minute|day|week)s?\s*(ago)?$", re.IGNORECASE),
    re.compile(r"^\d+\s+repl(y|ies)$", re.IGNORECASE),    # "3 replies"
    re.compile(r"^view\s+\d+\s+repl", re.IGNORECASE),     # "View 3 replies"
    re.compile(r"^most relevant", re.IGNORECASE),
    re.compile(r"^all reactions", re.IGNORECASE),
    re.compile(r"^\d+$"),                                   # bare numbers
    re.compile(r"^meta\s*[©(]", re.IGNORECASE),            # "Meta © 2026"
    re.compile(r"^see who reacted", re.IGNORECASE),
    re.compile(r"^log in or sign up", re.IGNORECASE),
    re.compile(r"^sign up to see", re.IGNORECASE),
    re.compile(r"^create an account", re.IGNORECASE),
    re.compile(r"^join facebook", re.IGNORECASE),
    re.compile(r"^\d+ (comment|share)s?$", re.IGNORECASE),
    re.compile(r"^see more of", re.IGNORECASE),
    re.compile(r"^(yesterday|today|just now)$", re.IGNORECASE),
    re.compile(r"^[·•\s\u00a0\u202f]+$"),                   # dot/bullet/whitespace lines
    re.compile(r"^privacy\s*·\s*terms", re.IGNORECASE),     # footer combos
    re.compile(r"replied\s*$", re.IGNORECASE),              # "X replied"
    re.compile(r"^author$", re.IGNORECASE),
    # "X replied | · | N Replies" pattern from extension data
    re.compile(r"replied\s*[\s|·\u00a0\u202f]*\d*\s*repl", re.IGNORECASE),
    # Timestamp-line: "January 25 at 6:56 PM | · | ·" or "February 1 at 1:04 PM | ..."
    re.compile(r"^[A-Z][a-z]+\s+\d{1,2}(?:,?\s+\d{4})?\s+at\s+\d+:\d+\s*(AM|PM)", re.IGNORECASE),
    # Bare date line: "January 22 | · | ·" (date + separators only)
    re.compile(r"^[A-Z][a-z]+\s+\d{1,2}(?:,?\s+\d{4})?\s*[|·\s\u00a0\u202f]*$", re.IGNORECASE),
    # Standalone day of week
    re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)$", re.IGNORECASE),
    # "Top fan" badge text
    re.compile(r"^top fan$", re.IGNORECASE),
]


def is_garbage_comment(author: str, text: str, page_name: str = "") -> bool:
    """Comprehensive check for non-comment noise captured from the page."""
    if not text:
        return True

    # Normalize unicode whitespace
    text_stripped = _normalize_whitespace(text).strip()
    text_lower = text_stripped.lower()

    # Too short
    if len(text_stripped) < 2:
        return True

    # Exact match noise
    if text_lower in _GARBAGE_EXACT:
        return True

    # Pattern matches
    if any(p.match(text_stripped) for p in _GARBAGE_PATTERNS):
        return True

    # Author is "Log In" (captured from page chrome)
    if author and author.strip().lower() == "log in":
        return True

    # Text equals the page name (header leak)
    if page_name and text_lower == page_name.lower().strip():
        return True

    # Text equals the author name (author-only leak)
    if author and text_lower == author.strip().lower():
        return True

    # Text is only separators and a relative timestamp (e.g. "6d | · | ·")
    collapsed = re.sub(r"[\s|·•\u00a0\u202f]+", " ", text_stripped).strip()
    if re.match(r"^\d+[hmdws]$", collapsed, re.IGNORECASE):
        return True

    # Text contains "replied" + separator + "Reply/Replies" (e.g. "Vote Kevin Crye replied | · | 1 Reply")
    if re.search(r"replied\b", text_stripped, re.IGNORECASE) and re.search(r"\d+\s+repl", text_stripped, re.IGNORECASE):
        return True

    # Name-only "comment" — just a person's name (2-3 capitalized words, no other content)
    # e.g. "John Ramirez", "Lori Keys-Peisker", "Echo Bongaarts"
    if re.match(r"^[A-Z][a-z]+(\s+[A-Z][a-z-]+){0,3}$", text_stripped) and len(text_stripped) < 40:
        return True

    return False


# ---------------------------------------------------------------------------
# Post-level orchestrator
# ---------------------------------------------------------------------------

def is_garbage_post(text: str, page_name: str = "") -> bool:
    """
    Returns True if the post text looks like a captured comment fragment
    rather than actual post content. Used to reject or delete bad posts.

    Catches patterns like:
      "Doug Hirsch\\nWelcome\\n1w"  (name + short text + relative timestamp)
      "John Ramirez\\n1y"           (name + relative timestamp)
    """
    if not text:
        return True

    text = _normalize_whitespace(text).strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    if not lines:
        return True

    # Single line that is just a name (2-3 capitalized words)
    if len(lines) == 1 and re.match(r"^[A-Z][a-z]+(\s+[A-Z][a-z-]+){0,3}$", lines[0]):
        return True

    # 2-3 lines where the last line is a relative timestamp
    if 2 <= len(lines) <= 4:
        last = lines[-1]
        if _RELATIVE_TS_RE.match(last):
            # Rest is likely a name + short comment text
            non_ts = " ".join(lines[:-1])
            if len(non_ts) < 60:
                return True

    return False


def sanitize_post(post_data: dict, page_name: str = "") -> Optional[dict]:
    """
    Clean a post dict. Returns None if the post should be skipped (login wall
    or garbage/comment fragment).

    post_data is a dict with keys like 'text', 'reaction_count', 'timestamp', etc.
    """
    text = post_data.get("text", "")

    # Reject login wall pages entirely
    if is_login_wall(text):
        return None

    # Strip page chrome from text
    post_data["text"] = strip_page_chrome(text, page_name)

    # Reject garbage posts (comment fragments captured as posts)
    if is_garbage_post(post_data["text"], page_name):
        return None

    # Clean reaction count
    rc = post_data.get("reaction_count", "")
    if rc:
        post_data["reaction_count"] = clean_reaction_count(rc)

    # Resolve timestamps to ISO format
    ts = post_data.get("timestamp", "")
    ts_raw = post_data.get("timestamp_raw", "")
    raw_to_resolve = ts_raw or ts
    if raw_to_resolve:
        # Try relative first ("6d", "3h")
        resolved = resolve_relative_timestamp(raw_to_resolve)
        if resolved != raw_to_resolve:
            post_data["timestamp"] = resolved
        else:
            # Try absolute ("February 6 at 6:00 PM", "7 January")
            dt = parse_fb_timestamp(raw_to_resolve)
            if dt is not None:
                post_data["timestamp"] = dt.isoformat()

    return post_data


def sanitize_comments(comments: list[dict], page_name: str = "") -> list[dict]:
    """Filter a list of comment dicts through garbage detection."""
    return [
        c for c in comments
        if not is_garbage_comment(c.get("author", ""), c.get("text", ""), page_name)
    ]
