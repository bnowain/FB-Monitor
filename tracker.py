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


def get_due_tracking_jobs(state: dict, recheck_minutes: int, tracking_hours: int) -> list[dict]:
    """
    Return tracking jobs that are:
    - Still within the tracking window (< tracking_hours since detection)
    - Due for a recheck (> recheck_minutes since last check, or never checked)
    """
    now = datetime.now(timezone.utc)
    due = []

    for job in state.get("active_tracking", []):
        detected = datetime.fromisoformat(job["detected_at"])
        age_hours = (now - detected).total_seconds() / 3600

        if age_hours > tracking_hours:
            continue  # Past tracking window

        last_check = job.get("last_comment_check")
        if last_check is None:
            due.append(job)
        else:
            last = datetime.fromisoformat(last_check)
            minutes_since = (now - last).total_seconds() / 60
            if minutes_since >= recheck_minutes:
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
    """Remove tracking jobs past the tracking window. Returns count removed."""
    now = datetime.now(timezone.utc)
    before = len(state.get("active_tracking", []))

    state["active_tracking"] = [
        job for job in state.get("active_tracking", [])
        if (now - datetime.fromisoformat(job["detected_at"])).total_seconds() / 3600 <= tracking_hours
    ]

    removed = before - len(state["active_tracking"])
    if removed > 0:
        log.info(f"  🧹 Pruned {removed} expired tracking job(s)")
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
