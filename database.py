"""
database.py — SQLite storage for FB-Monitor.

Stores posts, comments, and attachment metadata in a local SQLite database.
The scraper writes here alongside the existing JSON/file output, and the
web UI reads from it.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("fb-monitor")

DB_PATH = Path(__file__).parent / "fb_monitor.db"


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a SQLite connection with row factory enabled."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Optional[Path] = None):
    """Create tables if they don't exist."""
    conn = get_connection(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT UNIQUE NOT NULL,
            page_name TEXT NOT NULL,
            page_url TEXT,
            post_url TEXT NOT NULL,
            author TEXT,
            text TEXT,
            timestamp TEXT,
            timestamp_raw TEXT,
            shared_from TEXT,
            shared_original_url TEXT,
            links TEXT,
            reaction_count TEXT,
            comment_count_text TEXT,
            share_count_text TEXT,
            post_dir TEXT,
            account TEXT,
            detected_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL REFERENCES posts(post_id),
            author TEXT,
            text TEXT NOT NULL,
            timestamp TEXT,
            is_reply INTEGER DEFAULT 0,
            detected_at TEXT NOT NULL,
            UNIQUE(post_id, author, text)
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL REFERENCES posts(post_id),
            type TEXT NOT NULL,
            url TEXT,
            local_path TEXT,
            filename TEXT,
            downloaded_at TEXT
        );

        -- People: central entity linking pages, posts, and comments
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            facebook_url TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        );

        -- Link people to pages they operate/own
        CREATE TABLE IF NOT EXISTS people_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            page_name TEXT NOT NULL,
            role TEXT DEFAULT 'owner',
            UNIQUE(person_id, page_name)
        );

        -- Link people to individual posts (author, tagged, mentioned)
        CREATE TABLE IF NOT EXISTS people_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            post_id TEXT NOT NULL REFERENCES posts(post_id),
            role TEXT DEFAULT 'author',
            UNIQUE(person_id, post_id, role)
        );

        -- Link people to comments they authored
        CREATE TABLE IF NOT EXISTS people_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
            role TEXT DEFAULT 'author',
            UNIQUE(person_id, comment_id)
        );

        CREATE INDEX IF NOT EXISTS idx_posts_page ON posts(page_name);
        CREATE INDEX IF NOT EXISTS idx_posts_detected ON posts(detected_at);
        CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
        CREATE INDEX IF NOT EXISTS idx_attachments_post ON attachments(post_id);
        CREATE INDEX IF NOT EXISTS idx_people_name ON people(name);
        CREATE INDEX IF NOT EXISTS idx_people_pages_person ON people_pages(person_id);
        CREATE INDEX IF NOT EXISTS idx_people_posts_person ON people_posts(person_id);
        CREATE INDEX IF NOT EXISTS idx_people_posts_post ON people_posts(post_id);
        CREATE INDEX IF NOT EXISTS idx_people_comments_person ON people_comments(person_id);
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Write operations (used by the scraper)
# ---------------------------------------------------------------------------

def save_post(post_data: dict, account: str = "", db_path: Optional[Path] = None):
    """
    Insert or update a post in the database.

    post_data should be the dict from PostData.to_dict() with extra fields
    (detected_at, post_dir, attachments).
    """
    conn = get_connection(db_path)
    try:
        conn.execute("""
            INSERT INTO posts (
                post_id, page_name, page_url, post_url, author, text,
                timestamp, timestamp_raw, shared_from, shared_original_url,
                links, reaction_count, comment_count_text, share_count_text,
                post_dir, account, detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                text=excluded.text,
                timestamp=excluded.timestamp,
                reaction_count=excluded.reaction_count,
                comment_count_text=excluded.comment_count_text,
                share_count_text=excluded.share_count_text
        """, (
            post_data.get("post_id", ""),
            post_data.get("page_name", ""),
            post_data.get("page_url", ""),
            post_data.get("url", ""),
            post_data.get("author", ""),
            post_data.get("text", ""),
            post_data.get("timestamp", ""),
            post_data.get("timestamp_raw", ""),
            post_data.get("shared_from", ""),
            post_data.get("shared_original_url", ""),
            json.dumps(post_data.get("links", [])),
            post_data.get("reaction_count", ""),
            post_data.get("comment_count_text", ""),
            post_data.get("share_count_text", ""),
            post_data.get("post_dir", ""),
            account,
            post_data.get("detected_at", datetime.now(timezone.utc).isoformat()),
        ))
        conn.commit()
    except Exception as e:
        log.warning(f"DB: failed to save post {post_data.get('post_id', '?')}: {e}")
    finally:
        conn.close()


def save_comments(post_id: str, comments: list[dict], db_path: Optional[Path] = None):
    """
    Insert comments for a post (skipping duplicates).

    Each comment dict should have: author, text, timestamp, is_reply.
    """
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    try:
        for c in comments:
            try:
                conn.execute("""
                    INSERT INTO comments (post_id, author, text, timestamp, is_reply, detected_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(post_id, author, text) DO NOTHING
                """, (
                    post_id,
                    c.get("author", ""),
                    c.get("text", ""),
                    c.get("timestamp", ""),
                    1 if c.get("is_reply") else 0,
                    now,
                ))
                inserted += conn.total_changes
            except Exception:
                pass
        conn.commit()
    except Exception as e:
        log.warning(f"DB: failed to save comments for {post_id}: {e}")
    finally:
        conn.close()
    return inserted


def save_attachments(post_id: str, attachments: dict, db_path: Optional[Path] = None):
    """
    Save attachment metadata for a post.

    attachments dict should have "images" and "videos" lists of file paths.
    """
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        for img_path in attachments.get("images", []):
            p = Path(img_path)
            conn.execute("""
                INSERT OR IGNORE INTO attachments (post_id, type, local_path, filename, downloaded_at)
                VALUES (?, 'image', ?, ?, ?)
            """, (post_id, str(p), p.name, now))

        for vid_path in attachments.get("videos", []):
            p = Path(vid_path)
            conn.execute("""
                INSERT OR IGNORE INTO attachments (post_id, type, local_path, filename, downloaded_at)
                VALUES (?, 'video', ?, ?, ?)
            """, (post_id, str(p), p.name, now))

        conn.commit()
    except Exception as e:
        log.warning(f"DB: failed to save attachments for {post_id}: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read operations (used by the web UI)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# People operations
# ---------------------------------------------------------------------------

def create_person(name: str, facebook_url: str = "", notes: str = "", db_path: Optional[Path] = None) -> int:
    """Create a person and return their id."""
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        cursor = conn.execute(
            "INSERT INTO people (name, facebook_url, notes, created_at) VALUES (?, ?, ?, ?)",
            (name, facebook_url, notes, now),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_person(person_id: int, name: str = None, facebook_url: str = None, notes: str = None, db_path: Optional[Path] = None):
    """Update a person's details."""
    conn = get_connection(db_path)
    fields = []
    params = []
    if name is not None:
        fields.append("name=?")
        params.append(name)
    if facebook_url is not None:
        fields.append("facebook_url=?")
        params.append(facebook_url)
    if notes is not None:
        fields.append("notes=?")
        params.append(notes)
    if not fields:
        conn.close()
        return
    params.append(person_id)
    conn.execute(f"UPDATE people SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    conn.close()


def delete_person(person_id: int, db_path: Optional[Path] = None):
    """Delete a person and all their links (cascade)."""
    conn = get_connection(db_path)
    conn.execute("DELETE FROM people WHERE id=?", (person_id,))
    conn.commit()
    conn.close()


def get_people(search: str = "", db_path: Optional[Path] = None) -> list[dict]:
    """List all people, optionally filtered by name search."""
    conn = get_connection(db_path)
    if search:
        rows = conn.execute(
            "SELECT * FROM people WHERE name LIKE ? ORDER BY name",
            (f"%{search}%",),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM people ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_person(person_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    """Get a single person by id."""
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def link_person_to_page(person_id: int, page_name: str, role: str = "owner", db_path: Optional[Path] = None):
    """Link a person to a monitored page."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO people_pages (person_id, page_name, role) VALUES (?, ?, ?)",
        (person_id, page_name, role),
    )
    conn.commit()
    conn.close()


def unlink_person_from_page(person_id: int, page_name: str, db_path: Optional[Path] = None):
    """Remove a person-page link."""
    conn = get_connection(db_path)
    conn.execute(
        "DELETE FROM people_pages WHERE person_id=? AND page_name=?",
        (person_id, page_name),
    )
    conn.commit()
    conn.close()


def link_person_to_post(person_id: int, post_id: str, role: str = "author", db_path: Optional[Path] = None):
    """Link a person to a post (author, tagged, mentioned, etc)."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO people_posts (person_id, post_id, role) VALUES (?, ?, ?)",
        (person_id, post_id, role),
    )
    conn.commit()
    conn.close()


def unlink_person_from_post(person_id: int, post_id: str, role: str = "author", db_path: Optional[Path] = None):
    """Remove a person-post link."""
    conn = get_connection(db_path)
    conn.execute(
        "DELETE FROM people_posts WHERE person_id=? AND post_id=? AND role=?",
        (person_id, post_id, role),
    )
    conn.commit()
    conn.close()


def link_person_to_comment(person_id: int, comment_id: int, role: str = "author", db_path: Optional[Path] = None):
    """Link a person to a comment."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO people_comments (person_id, comment_id, role) VALUES (?, ?, ?)",
        (person_id, comment_id, role),
    )
    conn.commit()
    conn.close()


def get_person_pages(person_id: int, db_path: Optional[Path] = None) -> list[dict]:
    """Get all pages linked to a person."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM people_pages WHERE person_id=? ORDER BY page_name",
        (person_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_person_posts(person_id: int, db_path: Optional[Path] = None) -> list[dict]:
    """Get all posts linked to a person."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT p.*, pp.role
        FROM posts p
        JOIN people_posts pp ON p.post_id = pp.post_id
        WHERE pp.person_id = ?
        ORDER BY p.detected_at DESC
    """, (person_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_person_comments(person_id: int, db_path: Optional[Path] = None) -> list[dict]:
    """Get all comments linked to a person."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT c.*, pc.role
        FROM comments c
        JOIN people_comments pc ON c.id = pc.comment_id
        WHERE pc.person_id = ?
        ORDER BY c.detected_at DESC
    """, (person_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_people_for_post(post_id: str, db_path: Optional[Path] = None) -> list[dict]:
    """Get all people linked to a specific post."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT p.*, pp.role
        FROM people p
        JOIN people_posts pp ON p.id = pp.person_id
        WHERE pp.post_id = ?
        ORDER BY pp.role, p.name
    """, (post_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_people_for_page(page_name: str, db_path: Optional[Path] = None) -> list[dict]:
    """Get all people linked to a page."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT p.*, pp.role
        FROM people p
        JOIN people_pages pp ON p.id = pp.person_id
        WHERE pp.page_name = ?
        ORDER BY pp.role, p.name
    """, (page_name,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Read operations (used by the web UI)
# ---------------------------------------------------------------------------

def get_posts(
    page_name: str = "",
    search: str = "",
    limit: int = 50,
    offset: int = 0,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Fetch posts with optional filtering."""
    conn = get_connection(db_path)
    query = "SELECT * FROM posts WHERE 1=1"
    params = []

    if page_name:
        query += " AND page_name = ?"
        params.append(page_name)

    if search:
        query += " AND (text LIKE ? OR author LIKE ? OR shared_from LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term])

    query += " ORDER BY detected_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_post(post_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Fetch a single post by post_id."""
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_comments_for_post(post_id: str, db_path: Optional[Path] = None) -> list[dict]:
    """Fetch all comments for a post."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM comments WHERE post_id = ? ORDER BY detected_at, id",
        (post_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_attachments_for_post(post_id: str, db_path: Optional[Path] = None) -> list[dict]:
    """Fetch all attachments for a post."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM attachments WHERE post_id = ? ORDER BY type, id",
        (post_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_page_names(db_path: Optional[Path] = None) -> list[str]:
    """Get a list of all distinct page names in the database."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT DISTINCT page_name FROM posts ORDER BY page_name"
    ).fetchall()
    conn.close()
    return [r["page_name"] for r in rows]


def get_stats(db_path: Optional[Path] = None) -> dict:
    """Get summary statistics for the dashboard."""
    conn = get_connection(db_path)
    stats = {}

    stats["total_posts"] = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    stats["total_comments"] = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    stats["total_attachments"] = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    stats["total_pages"] = conn.execute(
        "SELECT COUNT(DISTINCT page_name) FROM posts"
    ).fetchone()[0]
    stats["total_people"] = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]

    # Recent activity
    stats["recent_posts"] = [dict(r) for r in conn.execute(
        "SELECT * FROM posts ORDER BY detected_at DESC LIMIT 10"
    ).fetchall()]

    # Posts per page
    stats["posts_per_page"] = [dict(r) for r in conn.execute(
        "SELECT page_name, COUNT(*) as count FROM posts GROUP BY page_name ORDER BY count DESC"
    ).fetchall()]

    conn.close()
    return stats
