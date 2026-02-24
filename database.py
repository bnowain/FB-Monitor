"""
database.py — SQLite storage for FB-Monitor.

Stores posts, comments, and attachment metadata in a local SQLite database.
The scraper writes here alongside the existing JSON/file output, and the
web UI reads from it.
"""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sanitize import (
    is_login_wall, is_garbage_post, strip_page_chrome, clean_reaction_count,
    resolve_relative_timestamp, is_garbage_comment,
)


def content_hash(text: str, media_urls: list[str] = None) -> str:
    """Generate a stable hash of post/comment content for edit detection."""
    normalized = (text or "").strip()
    media_part = "|".join(sorted(media_urls or []))
    payload = f"{normalized}\n{media_part}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

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


def _migrate_add_columns(conn: sqlite3.Connection):
    """Add new columns to existing tables (idempotent)."""
    migrations = [
        ("posts", "content_hash", "TEXT"),
        ("posts", "first_seen_at", "TEXT"),
        ("posts", "last_seen_at", "TEXT"),
        ("posts", "is_deleted", "INTEGER DEFAULT 0"),
        ("posts", "deleted_at", "TEXT"),
        ("comments", "content_hash", "TEXT"),
        ("comments", "first_seen_at", "TEXT"),
        ("comments", "last_seen_at", "TEXT"),
        ("comments", "is_deleted", "INTEGER DEFAULT 0"),
        ("comments", "deleted_at", "TEXT"),
        ("comments", "parent_comment_id", "INTEGER REFERENCES comments(id)"),
        ("comments", "root_comment_id", "INTEGER REFERENCES comments(id)"),
        ("comments", "depth", "INTEGER DEFAULT 0"),
    ]
    for table, column, col_type in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Backfill first_seen_at / last_seen_at from detected_at where NULL
    conn.execute("""
        UPDATE posts SET first_seen_at = detected_at
        WHERE first_seen_at IS NULL AND detected_at IS NOT NULL
    """)
    conn.execute("""
        UPDATE posts SET last_seen_at = detected_at
        WHERE last_seen_at IS NULL AND detected_at IS NOT NULL
    """)
    conn.execute("""
        UPDATE comments SET first_seen_at = detected_at
        WHERE first_seen_at IS NULL AND detected_at IS NOT NULL
    """)
    conn.execute("""
        UPDATE comments SET last_seen_at = detected_at
        WHERE last_seen_at IS NULL AND detected_at IS NOT NULL
    """)
    conn.commit()


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
            detected_at TEXT NOT NULL,
            content_hash TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            is_deleted INTEGER DEFAULT 0,
            deleted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL REFERENCES posts(post_id),
            author TEXT,
            text TEXT NOT NULL,
            timestamp TEXT,
            is_reply INTEGER DEFAULT 0,
            detected_at TEXT NOT NULL,
            content_hash TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            is_deleted INTEGER DEFAULT 0,
            deleted_at TEXT,
            parent_comment_id INTEGER REFERENCES comments(id),
            root_comment_id INTEGER REFERENCES comments(id),
            depth INTEGER DEFAULT 0,
            UNIQUE(post_id, author, text)
        );

        -- Track post edits over time
        CREATE TABLE IF NOT EXISTS post_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL REFERENCES posts(post_id),
            text TEXT,
            content_hash TEXT,
            links TEXT,
            reaction_count TEXT,
            comment_count_text TEXT,
            share_count_text TEXT,
            captured_at TEXT NOT NULL
        );

        -- Track comment edits over time
        CREATE TABLE IF NOT EXISTS comment_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id INTEGER NOT NULL REFERENCES comments(id),
            text TEXT,
            content_hash TEXT,
            captured_at TEXT NOT NULL
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

        -- Categories for classifying posts (e.g. Profiles, Tracking Pages)
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            color TEXT DEFAULT '#4f8ff7',
            created_at TEXT NOT NULL
        );

        -- Link posts to categories (many-to-many)
        CREATE TABLE IF NOT EXISTS post_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL REFERENCES posts(post_id),
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            UNIQUE(post_id, category_id)
        );

        -- Entities: organizations/groups that tie together pages and people
        -- e.g. "Shasta County HHSA"
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        );

        -- Link entities to monitored pages
        CREATE TABLE IF NOT EXISTS entity_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            page_name TEXT NOT NULL,
            UNIQUE(entity_id, page_name)
        );

        -- Link entities to people
        CREATE TABLE IF NOT EXISTS entity_people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            role TEXT DEFAULT 'member',
            UNIQUE(entity_id, person_id)
        );

        -- Import queue: manually submitted post URLs for anonymous scraping
        CREATE TABLE IF NOT EXISTS import_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            page_name TEXT,
            status TEXT DEFAULT 'pending',
            post_id TEXT,
            error TEXT,
            submitted_at TEXT NOT NULL,
            processed_at TEXT
        );

        -- Media download queue: logged-in account media flagged for manual review
        CREATE TABLE IF NOT EXISTS media_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL REFERENCES posts(post_id),
            url TEXT NOT NULL,
            type TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            local_path TEXT,
            account TEXT,
            created_at TEXT NOT NULL,
            downloaded_at TEXT,
            UNIQUE(post_id, url)
        );

        CREATE INDEX IF NOT EXISTS idx_posts_page ON posts(page_name);
        CREATE INDEX IF NOT EXISTS idx_posts_detected ON posts(detected_at);
        CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
        CREATE INDEX IF NOT EXISTS idx_attachments_post ON attachments(post_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attach_file ON attachments(post_id, type, filename) WHERE filename IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attach_url ON attachments(post_id, type, url) WHERE url IS NOT NULL AND filename IS NULL;
        CREATE INDEX IF NOT EXISTS idx_people_name ON people(name);
        CREATE INDEX IF NOT EXISTS idx_people_pages_person ON people_pages(person_id);
        CREATE INDEX IF NOT EXISTS idx_people_posts_person ON people_posts(person_id);
        CREATE INDEX IF NOT EXISTS idx_people_posts_post ON people_posts(post_id);
        CREATE INDEX IF NOT EXISTS idx_people_comments_person ON people_comments(person_id);
        CREATE INDEX IF NOT EXISTS idx_post_categories_post ON post_categories(post_id);
        CREATE INDEX IF NOT EXISTS idx_post_categories_cat ON post_categories(category_id);
        CREATE INDEX IF NOT EXISTS idx_entity_pages ON entity_pages(entity_id);
        CREATE INDEX IF NOT EXISTS idx_entity_people ON entity_people(entity_id);
        CREATE INDEX IF NOT EXISTS idx_import_queue_status ON import_queue(status);

        CREATE INDEX IF NOT EXISTS idx_posts_content_hash ON posts(content_hash);
        CREATE INDEX IF NOT EXISTS idx_posts_first_seen ON posts(first_seen_at);
        CREATE INDEX IF NOT EXISTS idx_posts_deleted ON posts(is_deleted);
        CREATE INDEX IF NOT EXISTS idx_comments_content_hash ON comments(content_hash);
        CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_comment_id);
        CREATE INDEX IF NOT EXISTS idx_comments_root ON comments(root_comment_id);
        CREATE INDEX IF NOT EXISTS idx_comments_deleted ON comments(is_deleted);
        CREATE INDEX IF NOT EXISTS idx_post_versions_post ON post_versions(post_id);
        CREATE INDEX IF NOT EXISTS idx_comment_versions_comment ON comment_versions(comment_id);
    """)
    conn.commit()

    # --- Migrate existing databases: add new columns if missing ---
    _migrate_add_columns(conn)

    # Seed default categories
    now = datetime.now(timezone.utc).isoformat()
    for name, desc, color in [
        ("Profiles", "Posts related to individual profiles", "#e89b3e"),
        ("Tracking Pages", "Posts from pages being actively tracked", "#4caf7d"),
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, description, color, created_at) VALUES (?, ?, ?, ?)",
            (name, desc, color, now),
        )
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

    On update: detects content edits via content_hash. If the post text has
    changed, saves the old version to post_versions before updating.
    """
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    post_id = post_data.get("post_id", "")
    text = post_data.get("text", "")
    image_urls = post_data.get("image_urls", [])
    video_urls = post_data.get("video_urls", [])
    media_urls = image_urls + video_urls
    new_hash = content_hash(text, media_urls)

    try:
        # Check if post already exists (for edit detection)
        existing = conn.execute(
            "SELECT id, text, content_hash, links, reaction_count, "
            "comment_count_text, share_count_text FROM posts WHERE post_id = ?",
            (post_id,),
        ).fetchone()

        if existing and existing["content_hash"] and existing["content_hash"] != new_hash:
            # Content changed — save old version
            conn.execute("""
                INSERT INTO post_versions
                    (post_id, text, content_hash, links, reaction_count,
                     comment_count_text, share_count_text, captured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                post_id,
                existing["text"],
                existing["content_hash"],
                existing["links"] if isinstance(existing["links"], str) else json.dumps(existing["links"] or []),
                existing["reaction_count"],
                existing["comment_count_text"],
                existing["share_count_text"],
                now,
            ))
            log.info(f"DB: post {post_id[:40]} edited — saved previous version")

        conn.execute("""
            INSERT INTO posts (
                post_id, page_name, page_url, post_url, author, text,
                timestamp, timestamp_raw, shared_from, shared_original_url,
                links, reaction_count, comment_count_text, share_count_text,
                post_dir, account, detected_at,
                content_hash, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                text=excluded.text,
                timestamp=excluded.timestamp,
                reaction_count=excluded.reaction_count,
                comment_count_text=excluded.comment_count_text,
                share_count_text=excluded.share_count_text,
                content_hash=excluded.content_hash,
                last_seen_at=excluded.last_seen_at
        """, (
            post_id,
            post_data.get("page_name", ""),
            post_data.get("page_url", ""),
            post_data.get("url", ""),
            post_data.get("author", ""),
            text,
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
            post_data.get("detected_at", now),
            new_hash,
            post_data.get("detected_at", now),  # first_seen_at (only on INSERT)
            now,  # last_seen_at (always updated)
        ))
        conn.commit()
    except Exception as e:
        log.warning(f"DB: failed to save post {post_id or '?'}: {e}")
    finally:
        conn.close()


def save_comments(post_id: str, comments: list[dict], db_path: Optional[Path] = None):
    """
    Insert comments for a post, with content hashing and edit detection.

    Each comment dict should have: author, text, timestamp, is_reply.
    Optional threading fields: depth, parent_comment_id, root_comment_id.

    On conflict (same post_id, author, text), updates last_seen_at.
    If an existing comment by the same author has a different content_hash,
    saves the old version to comment_versions.
    """
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    try:
        for c in comments:
            try:
                c_text = c.get("text", "")
                c_author = c.get("author", "")
                c_hash = content_hash(c_text)
                c_depth = c.get("depth", 0)
                c_is_reply = 1 if c.get("is_reply") else 0

                # If depth not explicitly set, infer from is_reply
                if c_depth == 0 and c_is_reply:
                    c_depth = 1

                conn.execute("""
                    INSERT INTO comments (
                        post_id, author, text, timestamp, is_reply, detected_at,
                        content_hash, first_seen_at, last_seen_at, depth
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(post_id, author, text) DO UPDATE SET
                        last_seen_at=excluded.last_seen_at,
                        content_hash=excluded.content_hash
                """, (
                    post_id,
                    c_author,
                    c_text,
                    c.get("timestamp", ""),
                    c_is_reply,
                    now,
                    c_hash,
                    now,  # first_seen_at (only on INSERT)
                    now,  # last_seen_at (always updated)
                    c_depth,
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

    attachments dict may have:
    - "images"/"videos": lists of downloaded file paths
    - "image_urls"/"video_urls": lists of source URLs (stored even without download)
    """
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        # Downloaded files (have local paths)
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

        # Video poster (thumbnail used as preview image)
        poster_path = attachments.get("poster")
        if poster_path:
            p = Path(poster_path)
            conn.execute("""
                INSERT OR IGNORE INTO attachments (post_id, type, local_path, filename, downloaded_at)
                VALUES (?, 'poster', ?, ?, ?)
            """, (post_id, str(p), p.name, now))

        # URL-only attachments (skip if we already have downloaded files of same type)
        if not attachments.get("images"):
            for img_url in attachments.get("image_urls", []):
                conn.execute("""
                    INSERT OR IGNORE INTO attachments (post_id, type, url, downloaded_at)
                    VALUES (?, 'image', ?, ?)
                """, (post_id, img_url, now))

        if not attachments.get("videos"):
            for vid_url in attachments.get("video_urls", []):
                conn.execute("""
                    INSERT OR IGNORE INTO attachments (post_id, type, url, downloaded_at)
                    VALUES (?, 'video', ?, ?)
                """, (post_id, vid_url, now))

        conn.commit()
    except Exception as e:
        log.warning(f"DB: failed to save attachments for {post_id}: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Edit / delete detection
# ---------------------------------------------------------------------------

def detect_deleted_comments(post_id: str, seen_comment_ids: set[int],
                            db_path: Optional[Path] = None) -> int:
    """
    Mark comments as deleted if they were previously visible but weren't
    found in the latest fetch.

    Uses a conservative approach: only tombstones comments that have been
    missing for 2+ consecutive checks (tracked via consecutive_misses in
    the comment_versions table as a side-channel).

    seen_comment_ids: set of comments.id values found in this fetch.
    Returns count of newly tombstoned comments.
    """
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    tombstoned = 0

    try:
        # Get all non-deleted comments for this post
        existing = conn.execute(
            "SELECT id, last_seen_at FROM comments "
            "WHERE post_id = ? AND is_deleted = 0",
            (post_id,),
        ).fetchall()

        for row in existing:
            cid = row["id"]
            if cid not in seen_comment_ids:
                # Comment wasn't seen — check how long it's been missing
                last_seen = row["last_seen_at"] or ""
                if last_seen:
                    try:
                        last_dt = datetime.fromisoformat(last_seen)
                        hours_missing = (
                            datetime.now(timezone.utc) - last_dt
                        ).total_seconds() / 3600
                        if hours_missing >= 48:
                            conn.execute(
                                "UPDATE comments SET is_deleted = 1, deleted_at = ? "
                                "WHERE id = ?",
                                (now, cid),
                            )
                            tombstoned += 1
                    except (ValueError, TypeError):
                        pass

        if tombstoned:
            conn.commit()
            log.info(f"DB: tombstoned {tombstoned} deleted comment(s) for {post_id[:40]}")
    except Exception as e:
        log.warning(f"DB: delete detection failed for {post_id}: {e}")
    finally:
        conn.close()

    return tombstoned


def detect_deleted_post(post_id: str, db_path: Optional[Path] = None) -> bool:
    """
    Mark a post as deleted (tombstone) if it was previously visible
    but is no longer accessible. Returns True if newly tombstoned.
    """
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        row = conn.execute(
            "SELECT is_deleted, last_seen_at FROM posts WHERE post_id = ?",
            (post_id,),
        ).fetchone()
        if row and not row["is_deleted"]:
            last_seen = row["last_seen_at"] or ""
            if last_seen:
                try:
                    last_dt = datetime.fromisoformat(last_seen)
                    hours_missing = (
                        datetime.now(timezone.utc) - last_dt
                    ).total_seconds() / 3600
                    if hours_missing >= 48:
                        conn.execute(
                            "UPDATE posts SET is_deleted = 1, deleted_at = ? "
                            "WHERE post_id = ?",
                            (now, post_id),
                        )
                        conn.commit()
                        log.info(f"DB: tombstoned deleted post {post_id[:40]}")
                        return True
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        log.warning(f"DB: delete detection failed for post {post_id}: {e}")
    finally:
        conn.close()
    return False


def get_post_versions(post_id: str, db_path: Optional[Path] = None) -> list[dict]:
    """Get all saved versions of a post (edit history)."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM post_versions WHERE post_id = ? ORDER BY captured_at DESC",
        (post_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_comment_versions(comment_id: int, db_path: Optional[Path] = None) -> list[dict]:
    """Get all saved versions of a comment (edit history)."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM comment_versions WHERE comment_id = ? ORDER BY captured_at DESC",
        (comment_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def touch_comment_last_seen(post_id: str, comment_ids: set[int],
                            db_path: Optional[Path] = None):
    """Update last_seen_at for comments that were found in the latest fetch."""
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        for cid in comment_ids:
            conn.execute(
                "UPDATE comments SET last_seen_at = ? WHERE id = ?",
                (now, cid),
            )
        conn.commit()
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
# Category operations
# ---------------------------------------------------------------------------

def get_categories(db_path: Optional[Path] = None) -> list[dict]:
    """Get all categories."""
    conn = get_connection(db_path)
    rows = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_category(category_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    """Get a single category."""
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_category(name: str, description: str = "", color: str = "#4f8ff7", db_path: Optional[Path] = None) -> int:
    """Create a category and return its id."""
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        cursor = conn.execute(
            "INSERT INTO categories (name, description, color, created_at) VALUES (?, ?, ?, ?)",
            (name, description, color, now),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_category(category_id: int, name: str = None, description: str = None, color: str = None, db_path: Optional[Path] = None):
    """Update a category."""
    conn = get_connection(db_path)
    fields, params = [], []
    if name is not None:
        fields.append("name=?"); params.append(name)
    if description is not None:
        fields.append("description=?"); params.append(description)
    if color is not None:
        fields.append("color=?"); params.append(color)
    if not fields:
        conn.close(); return
    params.append(category_id)
    conn.execute(f"UPDATE categories SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    conn.close()


def delete_category(category_id: int, db_path: Optional[Path] = None):
    """Delete a category (cascade removes post links)."""
    conn = get_connection(db_path)
    conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
    conn.commit()
    conn.close()


def tag_post_category(post_id: str, category_id: int, db_path: Optional[Path] = None):
    """Tag a post with a category."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO post_categories (post_id, category_id) VALUES (?, ?)",
        (post_id, category_id),
    )
    conn.commit()
    conn.close()


def untag_post_category(post_id: str, category_id: int, db_path: Optional[Path] = None):
    """Remove a category tag from a post."""
    conn = get_connection(db_path)
    conn.execute(
        "DELETE FROM post_categories WHERE post_id=? AND category_id=?",
        (post_id, category_id),
    )
    conn.commit()
    conn.close()


def get_categories_for_post(post_id: str, db_path: Optional[Path] = None) -> list[dict]:
    """Get all categories tagged on a post."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT c.*
        FROM categories c
        JOIN post_categories pc ON c.id = pc.category_id
        WHERE pc.post_id = ?
        ORDER BY c.name
    """, (post_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_posts_in_category(category_id: int, limit: int = 50, offset: int = 0, db_path: Optional[Path] = None) -> list[dict]:
    """Get all posts tagged with a category."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT p.*
        FROM posts p
        JOIN post_categories pc ON p.post_id = pc.post_id
        WHERE pc.category_id = ?
        ORDER BY p.detected_at DESC
        LIMIT ? OFFSET ?
    """, (category_id, limit, offset)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Entity operations
# ---------------------------------------------------------------------------

def get_entities(search: str = "", db_path: Optional[Path] = None) -> list[dict]:
    """List all entities."""
    conn = get_connection(db_path)
    if search:
        rows = conn.execute(
            "SELECT * FROM entities WHERE name LIKE ? ORDER BY name",
            (f"%{search}%",),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM entities ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_entity(entity_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    """Get a single entity."""
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_entity(name: str, description: str = "", db_path: Optional[Path] = None) -> int:
    """Create an entity and return its id."""
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        cursor = conn.execute(
            "INSERT INTO entities (name, description, created_at) VALUES (?, ?, ?)",
            (name, description, now),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_entity(entity_id: int, name: str = None, description: str = None, db_path: Optional[Path] = None):
    """Update an entity."""
    conn = get_connection(db_path)
    fields, params = [], []
    if name is not None:
        fields.append("name=?"); params.append(name)
    if description is not None:
        fields.append("description=?"); params.append(description)
    if not fields:
        conn.close(); return
    params.append(entity_id)
    conn.execute(f"UPDATE entities SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    conn.close()


def delete_entity(entity_id: int, db_path: Optional[Path] = None):
    """Delete an entity (cascade removes page/people links)."""
    conn = get_connection(db_path)
    conn.execute("DELETE FROM entities WHERE id=?", (entity_id,))
    conn.commit()
    conn.close()


def link_entity_to_page(entity_id: int, page_name: str, db_path: Optional[Path] = None):
    """Link an entity to a monitored page."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO entity_pages (entity_id, page_name) VALUES (?, ?)",
        (entity_id, page_name),
    )
    conn.commit()
    conn.close()


def unlink_entity_from_page(entity_id: int, page_name: str, db_path: Optional[Path] = None):
    """Remove an entity-page link."""
    conn = get_connection(db_path)
    conn.execute(
        "DELETE FROM entity_pages WHERE entity_id=? AND page_name=?",
        (entity_id, page_name),
    )
    conn.commit()
    conn.close()


def link_entity_to_person(entity_id: int, person_id: int, role: str = "member", db_path: Optional[Path] = None):
    """Link an entity to a person."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO entity_people (entity_id, person_id, role) VALUES (?, ?, ?)",
        (entity_id, person_id, role),
    )
    conn.commit()
    conn.close()


def unlink_entity_from_person(entity_id: int, person_id: int, db_path: Optional[Path] = None):
    """Remove an entity-person link."""
    conn = get_connection(db_path)
    conn.execute(
        "DELETE FROM entity_people WHERE entity_id=? AND person_id=?",
        (entity_id, person_id),
    )
    conn.commit()
    conn.close()


def get_entity_pages(entity_id: int, db_path: Optional[Path] = None) -> list[dict]:
    """Get all pages linked to an entity."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM entity_pages WHERE entity_id=? ORDER BY page_name",
        (entity_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_entity_people(entity_id: int, db_path: Optional[Path] = None) -> list[dict]:
    """Get all people linked to an entity."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT p.*, ep.role
        FROM people p
        JOIN entity_people ep ON p.id = ep.person_id
        WHERE ep.entity_id = ?
        ORDER BY ep.role, p.name
    """, (entity_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_entities_for_page(page_name: str, db_path: Optional[Path] = None) -> list[dict]:
    """Get all entities linked to a page."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT e.*
        FROM entities e
        JOIN entity_pages ep ON e.id = ep.entity_id
        WHERE ep.page_name = ?
        ORDER BY e.name
    """, (page_name,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_entities_for_person(person_id: int, db_path: Optional[Path] = None) -> list[dict]:
    """Get all entities a person belongs to."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT e.*, ep.role
        FROM entities e
        JOIN entity_people ep ON e.id = ep.entity_id
        WHERE ep.person_id = ?
        ORDER BY e.name
    """, (person_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Read operations (used by the web UI)
# ---------------------------------------------------------------------------

def get_page_stats(db_path: Optional[Path] = None) -> list[dict]:
    """Get aggregated stats for each monitored page."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT
            p.page_name,
            MAX(p.page_url) as page_url,
            COUNT(*) as total_posts,
            SUM(CASE WHEN p.author = p.page_name THEN 1 ELSE 0 END) as owner_posts,
            SUM(CASE WHEN p.author != p.page_name THEN 1 ELSE 0 END) as community_posts,
            MAX(p.detected_at) as latest_post_date,
            (SELECT COUNT(*) FROM people_pages pp WHERE pp.page_name = p.page_name) as people_count,
            (SELECT COUNT(*) FROM entity_pages ep WHERE ep.page_name = p.page_name) as entity_count
        FROM posts p
        GROUP BY p.page_name
        ORDER BY latest_post_date DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_posts(
    page_name: str = "",
    search: str = "",
    category_id: int = 0,
    entity_id: int = 0,
    limit: int = 50,
    offset: int = 0,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Fetch posts with optional filtering by page, search, category, or entity."""
    conn = get_connection(db_path)
    query = "SELECT DISTINCT p.* FROM posts p"
    joins = []
    wheres = []
    params = []

    if category_id:
        joins.append("JOIN post_categories pc ON p.post_id = pc.post_id")
        wheres.append("pc.category_id = ?")
        params.append(category_id)

    if entity_id:
        joins.append("JOIN entity_pages ep ON p.page_name = ep.page_name")
        wheres.append("ep.entity_id = ?")
        params.append(entity_id)

    if page_name:
        wheres.append("p.page_name = ?")
        params.append(page_name)

    if search:
        wheres.append("(p.text LIKE ? OR p.author LIKE ? OR p.shared_from LIKE ?)")
        term = f"%{search}%"
        params.extend([term, term, term])

    if joins:
        query += " " + " ".join(joins)
    if wheres:
        query += " WHERE " + " AND ".join(wheres)

    query += " ORDER BY p.detected_at DESC LIMIT ? OFFSET ?"
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
    stats["total_entities"] = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    stats["total_categories"] = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]

    stats["pending_downloads"] = conn.execute(
        "SELECT COUNT(*) FROM media_queue WHERE status='pending'"
    ).fetchone()[0]

    stats["pending_imports"] = conn.execute(
        "SELECT COUNT(*) FROM import_queue WHERE status='pending'"
    ).fetchone()[0]

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


# ---------------------------------------------------------------------------
# Media queue operations (pending downloads from logged-in accounts)
# ---------------------------------------------------------------------------

def queue_media(post_id: str, url: str, media_type: str, account: str = "", db_path: Optional[Path] = None):
    """Add a media URL to the pending download queue."""
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO media_queue (post_id, url, type, status, account, created_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
        """, (post_id, url, media_type, account, now))
        conn.commit()
    except Exception as e:
        log.warning(f"DB: failed to queue media for {post_id}: {e}")
    finally:
        conn.close()


def queue_media_batch(post_id: str, image_urls: list[str], video_urls: list[str],
                      post_url: str = "", account: str = "", db_path: Optional[Path] = None):
    """Queue multiple media URLs at once. For videos, stores the post_url for yt-dlp."""
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        for url in image_urls:
            conn.execute("""
                INSERT OR IGNORE INTO media_queue (post_id, url, type, status, account, created_at)
                VALUES (?, ?, 'image', 'pending', ?, ?)
            """, (post_id, url, account, now))
        for url in video_urls:
            conn.execute("""
                INSERT OR IGNORE INTO media_queue (post_id, url, type, status, account, created_at)
                VALUES (?, ?, 'video', 'pending', ?, ?)
            """, (post_id, url, account, now))
        # Also queue the post URL itself for yt-dlp video extraction
        if video_urls or any(p in post_url for p in ("/videos/", "/watch/", "/reel/")):
            conn.execute("""
                INSERT OR IGNORE INTO media_queue (post_id, url, type, status, account, created_at)
                VALUES (?, ?, 'video', 'pending', ?, ?)
            """, (post_id, post_url, account, now))
        conn.commit()
    except Exception as e:
        log.warning(f"DB: failed to queue media batch for {post_id}: {e}")
    finally:
        conn.close()


def get_pending_media(status: str = "pending", limit: int = 100, db_path: Optional[Path] = None) -> list[dict]:
    """Get queued media items, optionally filtered by status."""
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT mq.*, p.page_name, p.text as post_text, p.post_url as post_page_url
        FROM media_queue mq
        JOIN posts p ON mq.post_id = p.post_id
        WHERE mq.status = ?
        ORDER BY mq.created_at DESC
        LIMIT ?
    """, (status, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_media_queue_for_post(post_id: str, db_path: Optional[Path] = None) -> list[dict]:
    """Get all queued media for a specific post."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM media_queue WHERE post_id = ? ORDER BY type, id",
        (post_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_media_status(media_id: int, status: str, local_path: str = "", db_path: Optional[Path] = None):
    """Update a queued media item's status (pending -> downloaded/skipped)."""
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    if status == "downloaded":
        conn.execute(
            "UPDATE media_queue SET status=?, local_path=?, downloaded_at=? WHERE id=?",
            (status, local_path, now, media_id),
        )
    else:
        conn.execute(
            "UPDATE media_queue SET status=? WHERE id=?",
            (status, media_id),
        )
    conn.commit()
    conn.close()


def get_media_item(media_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    """Get a single media queue item."""
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM media_queue WHERE id=?", (media_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Import queue operations (URL backfill)
# ---------------------------------------------------------------------------

def add_import_urls(urls: list[str], page_name: str = "", db_path: Optional[Path] = None) -> int:
    """Add URLs to the import queue. Returns count of newly added URLs."""
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    try:
        for url in urls:
            url = url.strip()
            if not url:
                continue
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO import_queue (url, page_name, status, submitted_at) VALUES (?, ?, 'pending', ?)",
                    (url, page_name, now),
                )
                if conn.total_changes:
                    added += 1
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()
    return added


def get_import_queue(status: str = "", limit: int = 100, db_path: Optional[Path] = None) -> list[dict]:
    """Get import queue items, optionally filtered by status."""
    conn = get_connection(db_path)
    if status:
        rows = conn.execute(
            "SELECT * FROM import_queue WHERE status=? ORDER BY submitted_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM import_queue ORDER BY submitted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pending_imports(limit: int = 50, db_path: Optional[Path] = None) -> list[dict]:
    """Get pending import URLs for processing."""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM import_queue WHERE status='pending' ORDER BY submitted_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_import_status(import_id: int, status: str, post_id: str = "", error: str = "", db_path: Optional[Path] = None):
    """Update an import queue item's status."""
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE import_queue SET status=?, post_id=?, error=?, processed_at=? WHERE id=?",
        (status, post_id, error, now, import_id),
    )
    conn.commit()
    conn.close()


def delete_import(import_id: int, db_path: Optional[Path] = None):
    """Delete an import queue item."""
    conn = get_connection(db_path)
    conn.execute("DELETE FROM import_queue WHERE id=?", (import_id,))
    conn.commit()
    conn.close()


def get_import_counts(db_path: Optional[Path] = None) -> dict:
    """Get counts by status for the import queue."""
    conn = get_connection(db_path)
    counts = {
        "pending": conn.execute("SELECT COUNT(*) FROM import_queue WHERE status='pending'").fetchone()[0],
        "scraped": conn.execute("SELECT COUNT(*) FROM import_queue WHERE status='scraped'").fetchone()[0],
        "failed": conn.execute("SELECT COUNT(*) FROM import_queue WHERE status='failed'").fetchone()[0],
        "duplicate": conn.execute("SELECT COUNT(*) FROM import_queue WHERE status='duplicate'").fetchone()[0],
    }
    conn.close()
    return counts


# ---------------------------------------------------------------------------
# Data quality cleanup
# ---------------------------------------------------------------------------

def cleanup_bad_data(page_name: str = "", db_path: Optional[Path] = None) -> dict:
    """
    Clean existing data: delete login wall posts, strip chrome, resolve
    timestamps, clean reaction counts, delete garbage comments.

    If page_name is provided, only cleans that page. Otherwise cleans all.
    Returns a summary dict of actions taken.
    """
    conn = get_connection(db_path)
    results = {
        "login_wall_posts_deleted": 0,
        "garbage_posts_deleted": 0,
        "posts_chrome_stripped": 0,
        "posts_text_swapped": 0,
        "timestamps_resolved": 0,
        "reaction_counts_cleaned": 0,
        "garbage_comments_deleted": 0,
    }

    try:
        # Fetch posts to process
        if page_name:
            posts = conn.execute(
                "SELECT id, post_id, page_name, text, timestamp, timestamp_raw, "
                "reaction_count, detected_at FROM posts WHERE page_name = ?",
                (page_name,),
            ).fetchall()
        else:
            posts = conn.execute(
                "SELECT id, post_id, page_name, text, timestamp, timestamp_raw, "
                "reaction_count, detected_at FROM posts"
            ).fetchall()

        posts = [dict(r) for r in posts]
        delete_post_ids = []

        for post in posts:
            text = post.get("text", "") or ""
            pid = post["post_id"]
            pname = post.get("page_name", "")

            # 1. Delete login wall posts
            if is_login_wall(text):
                delete_post_ids.append(pid)
                results["login_wall_posts_deleted"] += 1
                continue

            # 2. Strip page chrome from text
            cleaned_text = strip_page_chrome(text, pname)
            if cleaned_text != text:
                conn.execute(
                    "UPDATE posts SET text = ? WHERE post_id = ?",
                    (cleaned_text, pid),
                )
                results["posts_chrome_stripped"] += 1
                text = cleaned_text  # use cleaned text for subsequent checks

            # 2b. Delete garbage posts (comment fragments captured as posts)
            if is_garbage_post(text, pname):
                delete_post_ids.append(pid)
                results["garbage_posts_deleted"] = results.get("garbage_posts_deleted", 0) + 1
                continue

            # 3. Resolve relative timestamps
            ts = post.get("timestamp", "") or ""
            ts_raw = post.get("timestamp_raw", "") or ""
            raw_to_resolve = ts_raw or ts
            if raw_to_resolve:
                # Use detected_at as reference date for existing data
                ref_date = None
                detected = post.get("detected_at", "")
                if detected:
                    try:
                        from dateutil.parser import parse as dateutil_parse
                        ref_date = dateutil_parse(detected)
                    except Exception:
                        try:
                            ref_date = datetime.fromisoformat(detected)
                        except Exception:
                            ref_date = None

                resolved = resolve_relative_timestamp(raw_to_resolve, ref_date)
                if resolved != raw_to_resolve:
                    conn.execute(
                        "UPDATE posts SET timestamp = ? WHERE post_id = ?",
                        (resolved, pid),
                    )
                    results["timestamps_resolved"] += 1

            # 4. Clean reaction counts
            rc = post.get("reaction_count", "") or ""
            if rc:
                cleaned_rc = clean_reaction_count(rc)
                if cleaned_rc != rc:
                    conn.execute(
                        "UPDATE posts SET reaction_count = ? WHERE post_id = ?",
                        (cleaned_rc, pid),
                    )
                    results["reaction_counts_cleaned"] += 1

        # Delete login wall posts and their related data
        for pid in delete_post_ids:
            conn.execute("DELETE FROM comments WHERE post_id = ?", (pid,))
            conn.execute("DELETE FROM attachments WHERE post_id = ?", (pid,))
            conn.execute("DELETE FROM people_posts WHERE post_id = ?", (pid,))
            conn.execute("DELETE FROM post_categories WHERE post_id = ?", (pid,))
            conn.execute("DELETE FROM media_queue WHERE post_id = ?", (pid,))
            conn.execute("DELETE FROM posts WHERE post_id = ?", (pid,))

        # 5. Fix swapped post text / comment text
        # When the post text is short and matches a comment, and a longer
        # comment looks like the real post body, swap them.
        results["posts_text_swapped"] = 0
        surviving_pids = [p["post_id"] for p in posts if p["post_id"] not in delete_post_ids]
        for pid in surviving_pids:
            post_row = conn.execute(
                "SELECT text FROM posts WHERE post_id = ?", (pid,)
            ).fetchone()
            if not post_row:
                continue
            post_text = (post_row["text"] or "").strip()

            # Get all comments for this post
            comment_rows = conn.execute(
                "SELECT id, text FROM comments WHERE post_id = ? ORDER BY id",
                (pid,),
            ).fetchall()
            if not comment_rows:
                continue

            comment_texts = [(r["id"], (r["text"] or "").strip()) for r in comment_rows]

            # Check: is the post text identical to one of its comments?
            post_matches_comment = any(ct == post_text for _, ct in comment_texts)
            if not post_matches_comment:
                continue

            # Find the longest comment — if it's substantially longer than post text,
            # it's probably the real post body that got swapped
            longest_cid, longest_ct = max(comment_texts, key=lambda x: len(x[1]))
            if len(longest_ct) <= len(post_text) or len(longest_ct) < 50:
                continue

            # Swap: set post text to the longest comment, delete that comment,
            # and add the old post text as a comment
            conn.execute(
                "UPDATE posts SET text = ? WHERE post_id = ?",
                (longest_ct, pid),
            )
            conn.execute("DELETE FROM comments WHERE id = ?", (longest_cid,))
            # Add old (short) post text as a comment (it was a real comment)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO comments (post_id, author, text, timestamp, is_reply, detected_at) "
                "VALUES (?, '', ?, '', 0, ?)",
                (pid, post_text, now),
            )
            results["posts_text_swapped"] += 1
            log.info(f"  Swapped post/comment text for {pid}: "
                     f"'{post_text[:40]}...' <-> '{longest_ct[:40]}...'")

        # 6. Delete garbage comments
        if page_name:
            comments = conn.execute(
                "SELECT c.id, c.author, c.text FROM comments c "
                "JOIN posts p ON c.post_id = p.post_id "
                "WHERE p.page_name = ?",
                (page_name,),
            ).fetchall()
        else:
            comments = conn.execute(
                "SELECT c.id, c.author, c.text FROM comments c "
                "JOIN posts p ON c.post_id = p.post_id"
            ).fetchall()

        garbage_ids = []
        for c in comments:
            c = dict(c)
            if is_garbage_comment(c.get("author", ""), c.get("text", ""), page_name):
                garbage_ids.append(c["id"])

        for cid in garbage_ids:
            conn.execute("DELETE FROM people_comments WHERE comment_id = ?", (cid,))
            conn.execute("DELETE FROM comments WHERE id = ?", (cid,))

        results["garbage_comments_deleted"] = len(garbage_ids)

        conn.commit()

    except Exception as e:
        log.error(f"Cleanup failed: {e}")
        conn.rollback()
        results["error"] = str(e)
    finally:
        conn.close()

    return results


def backfill_image_urls(db_path: Optional[Path] = None) -> dict:
    """
    Scan post.json files on disk and backfill image/video URLs into the
    attachments table for posts that have no attachment rows yet.
    """
    conn = get_connection(db_path)
    results = {"scanned": 0, "backfilled": 0, "urls_added": 0}

    try:
        rows = conn.execute(
            "SELECT post_id, post_dir FROM posts WHERE post_dir IS NOT NULL AND post_dir != ''"
        ).fetchall()

        for row in rows:
            post_id = row["post_id"]
            post_dir = Path(row["post_dir"])
            post_json = post_dir / "post.json"

            if not post_json.exists():
                continue
            results["scanned"] += 1

            # Check if this post already has attachments
            existing = conn.execute(
                "SELECT COUNT(*) FROM attachments WHERE post_id=?", (post_id,)
            ).fetchone()[0]
            if existing > 0:
                continue

            try:
                with open(post_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue

            # Collect URLs from both top-level and attachments sub-dict
            image_urls = set(data.get("image_urls", []))
            video_urls = set(data.get("video_urls", []))
            att = data.get("attachments", {})
            if isinstance(att, dict):
                image_urls.update(att.get("image_urls", []))
                video_urls.update(att.get("video_urls", []))

            if not image_urls and not video_urls:
                continue

            now = datetime.now(timezone.utc).isoformat()
            added = 0
            for url in image_urls:
                if url:
                    conn.execute(
                        "INSERT OR IGNORE INTO attachments (post_id, type, url, downloaded_at) VALUES (?, 'image', ?, ?)",
                        (post_id, url, now),
                    )
                    added += 1
            for url in video_urls:
                if url:
                    conn.execute(
                        "INSERT OR IGNORE INTO attachments (post_id, type, url, downloaded_at) VALUES (?, 'video', ?, ?)",
                        (post_id, url, now),
                    )
                    added += 1

            if added:
                results["backfilled"] += 1
                results["urls_added"] += added

        conn.commit()
    except Exception as e:
        log.error(f"Backfill failed: {e}")
        results["error"] = str(e)
    finally:
        conn.close()

    return results
