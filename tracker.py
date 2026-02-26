"""
tracker.py — Persistent state management.

Tracks:
- Which posts have been detected (so we don't re-process them)
- Which posts are actively being monitored for new comments
- When each post was first detected and last checked
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("fb-monitor")

STATE_FILE = Path(__file__).parent / "state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"seen_posts": {}, "active_tracking": []}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def is_post_seen(state: dict, page_key: str, post_id: str) -> bool:
    return post_id in state.get("seen_posts", {}).get(page_key, [])


def mark_post_seen(state: dict, page_key: str, post_id: str):
    if page_key not in state["seen_posts"]:
        state["seen_posts"][page_key] = []
    if post_id not in state["seen_posts"][page_key]:
        state["seen_posts"][page_key].append(post_id)


def add_tracking_job(state: dict, post_id: str, post_url: str, post_dir: str, page_name: str, account: str = "anonymous"):
    """Register a post for ongoing comment monitoring."""
    job = {
        "post_id": post_id,
        "post_url": post_url,
        "post_dir": post_dir,
        "page_name": page_name,
        "account": account,
        "detected_at": _now(),
        "last_comment_check": None,
        "comment_checks": 0,
    }
    state.setdefault("active_tracking", [])

    # Don't duplicate
    existing_ids = {j["post_id"] for j in state["active_tracking"]}
    if post_id not in existing_ids:
        state["active_tracking"].append(job)
        log.info(f"  📋 Tracking comments for post {post_id}")


# ---------------------------------------------------------------------------
# Lookback scheduling tiers
# ---------------------------------------------------------------------------
# Posts are rechecked at decreasing frequencies as they age:
#   0-24h:  "active" window — uses recheck_minutes interval
#   24-48h: "lookback_48h" — every 6 hours
#   48h-7d: "lookback_7d"  — every 24 hours
#   7d-30d: "lookback_30d" — every 7 days (168 hours)
#
# The get_due_tracking_jobs function now applies tiered intervals.

LOOKBACK_TIERS = [
    # (max_age_hours, recheck_interval_minutes, tier_name)
    (24,   None,  "active"),       # uses the caller-supplied recheck_minutes
    (48,   360,   "lookback_48h"), # 6 hours
    (168,  1440,  "lookback_7d"),  # 24 hours
    (720,  10080, "lookback_30d"), # 7 days
]


def get_due_tracking_jobs(state: dict, recheck_minutes: int, tracking_hours: int) -> list[dict]:
    """
    Return tracking jobs that are due for a recheck, using tiered scheduling.

    Jobs within the active window (0-24h) use recheck_minutes.
    Older jobs use progressively longer intervals defined in LOOKBACK_TIERS.
    Jobs older than 30 days are not rechecked (pruned separately).
    """
    now = datetime.now(timezone.utc)
    due = []
    max_tracking_hours = max(tracking_hours, LOOKBACK_TIERS[-1][0])

    for job in state.get("active_tracking", []):
        detected = datetime.fromisoformat(job["detected_at"])
        age_hours = (now - detected).total_seconds() / 3600

        if age_hours > max_tracking_hours:
            continue

        # Determine the appropriate recheck interval for this job's age
        interval = recheck_minutes
        for tier_max, tier_interval, tier_name in LOOKBACK_TIERS:
            if age_hours <= tier_max:
                interval = tier_interval if tier_interval is not None else recheck_minutes
                break

        last_check = job.get("last_comment_check")
        if last_check is None:
            due.append(job)
        else:
            last = datetime.fromisoformat(last_check)
            minutes_since = (now - last).total_seconds() / 60
            if minutes_since >= interval:
                due.append(job)

    return due


def update_tracking_job(state: dict, post_id: str):
    """Mark a tracking job as just checked."""
    for job in state.get("active_tracking", []):
        if job["post_id"] == post_id:
            job["last_comment_check"] = _now()
            job["comment_checks"] = job.get("comment_checks", 0) + 1
            break


def prune_expired_jobs(state: dict, tracking_hours: int) -> int:
    """
    Remove tracking jobs past the maximum lookback window (30 days).

    The tracking_hours parameter sets the active window, but jobs are
    kept alive through the lookback tiers (up to 30 days / 720 hours).
    """
    now = datetime.now(timezone.utc)
    max_hours = max(tracking_hours, LOOKBACK_TIERS[-1][0])
    before = len(state.get("active_tracking", []))

    state["active_tracking"] = [
        job for job in state.get("active_tracking", [])
        if (now - datetime.fromisoformat(job["detected_at"])).total_seconds() / 3600 <= max_hours
    ]

    removed = before - len(state["active_tracking"])
    if removed > 0:
        log.info(f"  Pruned {removed} expired tracking job(s) (>{max_hours}h old)")
    return removed


def get_tracking_summary(state: dict, tracking_hours: int) -> str:
    """Human-readable summary of tracking state."""
    now = datetime.now(timezone.utc)
    active = state.get("active_tracking", [])
    seen_total = sum(len(v) for v in state.get("seen_posts", {}).values())

    lines = [
        f"Total posts seen: {seen_total}",
        f"Active comment tracking jobs: {len(active)}",
    ]

    for job in active:
        detected = datetime.fromisoformat(job["detected_at"])
        age = now - detected
        hours_left = tracking_hours - (age.total_seconds() / 3600)
        checks = job.get("comment_checks", 0)
        lines.append(
            f"  - {job['page_name']}: {job['post_id'][:20]}... "
            f"({checks} checks, {hours_left:.1f}h remaining)"
        )

    return "\n".join(lines)
