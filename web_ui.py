#!/usr/bin/env python3
"""
web_ui.py — FastAPI web interface for FB-Monitor.

Browse captured posts, comments, and attachments.

Usage:
    python web_ui.py                    # Start on port 8000
    python web_ui.py --port 9000        # Custom port
    python web_ui.py --host 0.0.0.0     # Listen on all interfaces
"""

import argparse
import hashlib
import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Body, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import base64

import database as db
from downloader import download_images, download_video_ytdlp

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="FB Monitor")
log = logging.getLogger("fb-monitor-web")


# ---------------------------------------------------------------------------
# Pages (HTML)
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    stats = db.get_stats()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats,
        "active_page": "dashboard",
    })


@app.get("/posts", response_class=HTMLResponse)
async def posts_list(
    request: Request,
    page_name: str = Query("", alias="page_name"),
    search: str = Query(""),
    category_id: int = Query(0),
    entity_id: int = Query(0),
    offset: int = Query(0),
):
    posts = db.get_posts(
        page_name=page_name, search=search,
        category_id=category_id, entity_id=entity_id, offset=offset,
    )
    page_names = db.get_page_names()
    categories = db.get_categories()
    entities = db.get_entities()

    # Enrich each post with attachment counts, comment count, and categories
    conn = db.get_connection()
    for post in posts:
        pid = post["post_id"]
        img_count = conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE post_id=? AND type='image'", (pid,)
        ).fetchone()[0]
        vid_count = conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE post_id=? AND type='video'", (pid,)
        ).fetchone()[0]
        comment_count = conn.execute(
            "SELECT COUNT(*) FROM comments WHERE post_id=?", (pid,)
        ).fetchone()[0]
        post["attachment_counts"] = {"images": img_count, "videos": vid_count}
        post["comment_count"] = comment_count
        post["categories"] = db.get_categories_for_post(pid)
    conn.close()

    return templates.TemplateResponse("posts.html", {
        "request": request,
        "posts": posts,
        "page_names": page_names,
        "page_name": page_name,
        "search": search,
        "category_id": category_id,
        "entity_id": entity_id,
        "categories": categories,
        "entities": entities,
        "offset": offset,
        "active_page": "posts",
    })


@app.get("/posts/{post_id}", response_class=HTMLResponse)
async def post_detail(request: Request, post_id: str):
    post = db.get_post(post_id)
    if not post:
        return HTMLResponse("<h1>Post not found</h1>", status_code=404)

    comments = db.get_comments_for_post(post_id)
    attachments = db.get_attachments_for_post(post_id)

    # Parse links JSON
    links = []
    if post.get("links"):
        try:
            links = json.loads(post["links"])
        except (json.JSONDecodeError, TypeError):
            pass

    linked_people = db.get_people_for_post(post_id)
    all_people = db.get_people()
    post_categories = db.get_categories_for_post(post_id)
    all_categories = db.get_categories()
    queued_media = db.get_media_queue_for_post(post_id)

    return templates.TemplateResponse("post_detail.html", {
        "request": request,
        "post": post,
        "comments": comments,
        "attachments": attachments,
        "links": links,
        "linked_people": linked_people,
        "all_people": all_people,
        "post_categories": post_categories,
        "all_categories": all_categories,
        "queued_media": queued_media,
        "active_page": "posts",
    })


# ---------------------------------------------------------------------------
# People (HTML)
# ---------------------------------------------------------------------------

@app.get("/people", response_class=HTMLResponse)
async def people_list(request: Request, search: str = ""):
    people = db.get_people(search=search)

    # Enrich with counts
    conn = db.get_connection()
    for person in people:
        pid = person["id"]
        person["page_count"] = conn.execute(
            "SELECT COUNT(*) FROM people_pages WHERE person_id=?", (pid,)
        ).fetchone()[0]
        person["post_count"] = conn.execute(
            "SELECT COUNT(*) FROM people_posts WHERE person_id=?", (pid,)
        ).fetchone()[0]
        person["comment_count"] = conn.execute(
            "SELECT COUNT(*) FROM people_comments WHERE person_id=?", (pid,)
        ).fetchone()[0]
    conn.close()

    return templates.TemplateResponse("people.html", {
        "request": request,
        "people": people,
        "search": search,
        "active_page": "people",
    })


@app.post("/people/create")
async def create_person(
    name: str = Form(...),
    facebook_url: str = Form(""),
    notes: str = Form(""),
):
    db.create_person(name, facebook_url, notes)
    return RedirectResponse("/people", status_code=303)


@app.get("/people/{person_id}", response_class=HTMLResponse)
async def person_detail(request: Request, person_id: int):
    person = db.get_person(person_id)
    if not person:
        return HTMLResponse("<h1>Person not found</h1>", status_code=404)

    pages = db.get_person_pages(person_id)
    posts = db.get_person_posts(person_id)
    comments = db.get_person_comments(person_id)
    all_page_names = db.get_page_names()
    person_entities = db.get_entities_for_person(person_id)
    all_entities = db.get_entities()

    return templates.TemplateResponse("person_detail.html", {
        "request": request,
        "person": person,
        "pages": pages,
        "posts": posts,
        "comments": comments,
        "all_page_names": all_page_names,
        "person_entities": person_entities,
        "all_entities": all_entities,
        "active_page": "people",
    })


@app.post("/people/{person_id}/update")
async def update_person(
    person_id: int,
    name: str = Form(...),
    facebook_url: str = Form(""),
    notes: str = Form(""),
):
    db.update_person(person_id, name=name, facebook_url=facebook_url, notes=notes)
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@app.post("/people/{person_id}/delete")
async def delete_person_route(person_id: int):
    db.delete_person(person_id)
    return RedirectResponse("/people", status_code=303)


@app.post("/people/{person_id}/link-page")
async def link_page(
    person_id: int,
    page_name: str = Form(...),
    role: str = Form("owner"),
):
    db.link_person_to_page(person_id, page_name, role)
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@app.post("/people/{person_id}/unlink-page")
async def unlink_page(person_id: int, page_name: str = Form(...)):
    db.unlink_person_from_page(person_id, page_name)
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@app.post("/people/{person_id}/link-post")
async def link_post(
    person_id: int,
    post_id: str = Form(...),
    role: str = Form("mentioned"),
):
    db.link_person_to_post(person_id, post_id, role)
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@app.post("/posts/{post_id}/link-person")
async def link_person_to_post_route(
    post_id: str,
    person_id: int = Form(...),
    role: str = Form("mentioned"),
):
    db.link_person_to_post(person_id, post_id, role)
    return RedirectResponse(f"/posts/{post_id}", status_code=303)


@app.post("/posts/{post_id}/unlink-person")
async def unlink_person_from_post_route(
    post_id: str,
    person_id: int = Form(...),
    role: str = Form(""),
):
    db.unlink_person_from_post(person_id, post_id, role)
    return RedirectResponse(f"/posts/{post_id}", status_code=303)


# ---------------------------------------------------------------------------
# Post category tagging
# ---------------------------------------------------------------------------

@app.post("/posts/{post_id}/tag-category")
async def tag_post_with_category(
    post_id: str,
    category_id: int = Form(...),
):
    db.tag_post_category(post_id, category_id)
    return RedirectResponse(f"/posts/{post_id}", status_code=303)


@app.post("/posts/{post_id}/untag-category")
async def untag_post_category_route(
    post_id: str,
    category_id: int = Form(...),
):
    db.untag_post_category(post_id, category_id)
    return RedirectResponse(f"/posts/{post_id}", status_code=303)


# ---------------------------------------------------------------------------
# Entities (HTML)
# ---------------------------------------------------------------------------

@app.get("/entities", response_class=HTMLResponse)
async def entities_list(request: Request, search: str = ""):
    entities = db.get_entities(search=search)

    conn = db.get_connection()
    for entity in entities:
        eid = entity["id"]
        entity["page_count"] = conn.execute(
            "SELECT COUNT(*) FROM entity_pages WHERE entity_id=?", (eid,)
        ).fetchone()[0]
        entity["people_count"] = conn.execute(
            "SELECT COUNT(*) FROM entity_people WHERE entity_id=?", (eid,)
        ).fetchone()[0]
    conn.close()

    return templates.TemplateResponse("entities.html", {
        "request": request,
        "entities": entities,
        "search": search,
        "active_page": "entities",
    })


@app.post("/entities/create")
async def create_entity_route(
    name: str = Form(...),
    description: str = Form(""),
):
    db.create_entity(name, description)
    return RedirectResponse("/entities", status_code=303)


@app.get("/entities/{entity_id}", response_class=HTMLResponse)
async def entity_detail(request: Request, entity_id: int):
    entity = db.get_entity(entity_id)
    if not entity:
        return HTMLResponse("<h1>Entity not found</h1>", status_code=404)

    pages = db.get_entity_pages(entity_id)
    people = db.get_entity_people(entity_id)
    all_page_names = db.get_page_names()
    all_people = db.get_people()

    # Get posts from entity's linked pages
    entity_posts = db.get_posts(entity_id=entity_id, limit=20)

    return templates.TemplateResponse("entity_detail.html", {
        "request": request,
        "entity": entity,
        "pages": pages,
        "people": people,
        "posts": entity_posts,
        "all_page_names": all_page_names,
        "all_people": all_people,
        "active_page": "entities",
    })


@app.post("/entities/{entity_id}/update")
async def update_entity_route(
    entity_id: int,
    name: str = Form(...),
    description: str = Form(""),
):
    db.update_entity(entity_id, name=name, description=description)
    return RedirectResponse(f"/entities/{entity_id}", status_code=303)


@app.post("/entities/{entity_id}/delete")
async def delete_entity_route(entity_id: int):
    db.delete_entity(entity_id)
    return RedirectResponse("/entities", status_code=303)


@app.post("/entities/{entity_id}/link-page")
async def entity_link_page(
    entity_id: int,
    page_name: str = Form(...),
):
    db.link_entity_to_page(entity_id, page_name)
    return RedirectResponse(f"/entities/{entity_id}", status_code=303)


@app.post("/entities/{entity_id}/unlink-page")
async def entity_unlink_page(
    entity_id: int,
    page_name: str = Form(...),
):
    db.unlink_entity_from_page(entity_id, page_name)
    return RedirectResponse(f"/entities/{entity_id}", status_code=303)


@app.post("/entities/{entity_id}/link-person")
async def entity_link_person(
    entity_id: int,
    person_id: int = Form(...),
    role: str = Form("member"),
):
    db.link_entity_to_person(entity_id, person_id, role)
    return RedirectResponse(f"/entities/{entity_id}", status_code=303)


@app.post("/entities/{entity_id}/unlink-person")
async def entity_unlink_person(
    entity_id: int,
    person_id: int = Form(...),
):
    db.unlink_entity_from_person(entity_id, person_id)
    return RedirectResponse(f"/entities/{entity_id}", status_code=303)


# ---------------------------------------------------------------------------
# Categories (HTML)
# ---------------------------------------------------------------------------

@app.get("/categories", response_class=HTMLResponse)
async def categories_list(request: Request):
    categories = db.get_categories()

    conn = db.get_connection()
    for cat in categories:
        cat["post_count"] = conn.execute(
            "SELECT COUNT(*) FROM post_categories WHERE category_id=?", (cat["id"],)
        ).fetchone()[0]
    conn.close()

    return templates.TemplateResponse("categories.html", {
        "request": request,
        "categories": categories,
        "active_page": "categories",
    })


@app.post("/categories/create")
async def create_category_route(
    name: str = Form(...),
    description: str = Form(""),
    color: str = Form("#4f8ff7"),
):
    db.create_category(name, description, color)
    return RedirectResponse("/categories", status_code=303)


@app.post("/categories/{category_id}/update")
async def update_category_route(
    category_id: int,
    name: str = Form(...),
    description: str = Form(""),
    color: str = Form("#4f8ff7"),
):
    db.update_category(category_id, name=name, description=description, color=color)
    return RedirectResponse("/categories", status_code=303)


@app.post("/categories/{category_id}/delete")
async def delete_category_route(category_id: int):
    db.delete_category(category_id)
    return RedirectResponse("/categories", status_code=303)


# ---------------------------------------------------------------------------
# Person-entity linking from person detail
# ---------------------------------------------------------------------------

@app.post("/people/{person_id}/link-entity")
async def person_link_entity(
    person_id: int,
    entity_id: int = Form(...),
    role: str = Form("member"),
):
    db.link_entity_to_person(entity_id, person_id, role)
    return RedirectResponse(f"/people/{person_id}", status_code=303)


@app.post("/people/{person_id}/unlink-entity")
async def person_unlink_entity(
    person_id: int,
    entity_id: int = Form(...),
):
    db.unlink_entity_from_person(entity_id, person_id)
    return RedirectResponse(f"/people/{person_id}", status_code=303)


# ---------------------------------------------------------------------------
# Downloads queue (pending media from logged-in accounts)
# ---------------------------------------------------------------------------

@app.get("/downloads", response_class=HTMLResponse)
async def downloads_page(request: Request, status: str = "pending"):
    if status not in ("pending", "downloaded", "skipped"):
        status = "pending"
    items = db.get_pending_media(status=status)

    # Get counts for the status tabs
    conn = db.get_connection()
    counts = {
        "pending": conn.execute("SELECT COUNT(*) FROM media_queue WHERE status='pending'").fetchone()[0],
        "downloaded": conn.execute("SELECT COUNT(*) FROM media_queue WHERE status='downloaded'").fetchone()[0],
        "skipped": conn.execute("SELECT COUNT(*) FROM media_queue WHERE status='skipped'").fetchone()[0],
    }
    conn.close()

    # Count pending videos specifically for batch controls
    conn_v = db.get_connection()
    video_count = conn_v.execute(
        "SELECT COUNT(*) FROM media_queue WHERE status='pending' AND type='video'"
    ).fetchone()[0]
    conn_v.close()

    return templates.TemplateResponse("downloads.html", {
        "request": request,
        "items": items,
        "status": status,
        "counts": counts,
        "pending_videos": video_count,
        "batch_state": _batch_state,
        "active_page": "downloads",
    })


def _do_download(media_item: dict):
    """Execute the actual download in a background thread."""
    try:
        post = db.get_post(media_item["post_id"])
        if not post:
            db.update_media_status(media_item["id"], "skipped")
            return

        post_dir = Path(post.get("post_dir", ""))
        if not post_dir.exists():
            post_dir.mkdir(parents=True, exist_ok=True)
        attachments_dir = post_dir / "attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)

        # Load config for proxy settings
        config_path = Path(__file__).parent / "config.json"
        dl_proxy_config = None
        if config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
            dl_proxy_config = config.get("download_proxy")
            if dl_proxy_config and not dl_proxy_config.get("url"):
                dl_proxy_config = None

        if media_item["type"] == "image":
            saved = download_images(
                [media_item["url"]], attachments_dir,
                download_proxy=dl_proxy_config,
            )
            if saved:
                db.update_media_status(media_item["id"], "downloaded", saved[0])
                db.save_attachments(media_item["post_id"], {"images": saved, "videos": []})
            else:
                db.update_media_status(media_item["id"], "skipped")
        else:
            # Video — use the URL as the post URL for yt-dlp
            saved = download_video_ytdlp(
                media_item["url"], attachments_dir,
                download_proxy=dl_proxy_config,
            )
            if saved:
                db.update_media_status(media_item["id"], "downloaded", saved[0])
                db.save_attachments(media_item["post_id"], {"images": [], "videos": saved})
            else:
                db.update_media_status(media_item["id"], "skipped")

    except Exception as e:
        log.error(f"Download failed for media {media_item['id']}: {e}")


@app.post("/downloads/{media_id}/download")
async def trigger_download(media_id: int):
    item = db.get_media_item(media_id)
    if not item:
        return HTMLResponse("Not found", status_code=404)

    # Run download in background thread so the UI doesn't block
    thread = threading.Thread(target=_do_download, args=(item,), daemon=True)
    thread.start()

    # Mark as in-progress visually (it'll update to downloaded/skipped when done)
    db.update_media_status(media_id, "downloaded")

    return RedirectResponse("/downloads?status=pending", status_code=303)


@app.post("/downloads/{media_id}/skip")
async def skip_download(media_id: int):
    db.update_media_status(media_id, "skipped")
    return RedirectResponse("/downloads?status=pending", status_code=303)


# ---------------------------------------------------------------------------
# Batch video download with random intervals
# ---------------------------------------------------------------------------

_batch_state = {"running": False, "downloaded": 0, "total": 0, "current": ""}


def _batch_video_worker(min_delay: int, max_delay: int):
    """Download all pending videos one at a time with random delays between them."""
    try:
        conn = db.get_connection()
        items = conn.execute("""
            SELECT mq.* FROM media_queue mq
            WHERE mq.status = 'pending' AND mq.type = 'video'
            ORDER BY mq.created_at ASC
        """).fetchall()
        conn.close()
        items = [dict(r) for r in items]

        _batch_state["total"] = len(items)
        _batch_state["downloaded"] = 0

        for i, item in enumerate(items):
            if not _batch_state["running"]:
                log.info("Batch video download stopped by user")
                break

            _batch_state["current"] = item["url"][:80]
            log.info(f"Batch video [{i+1}/{len(items)}]: {item['url'][:80]}")

            _do_download(item)
            _batch_state["downloaded"] = i + 1

            # Random delay before next download
            if i < len(items) - 1 and _batch_state["running"]:
                delay = random.uniform(min_delay, max_delay)
                log.info(f"  Next download in {delay:.0f}s")
                # Sleep in small chunks so we can check for stop signal
                elapsed = 0
                while elapsed < delay and _batch_state["running"]:
                    time.sleep(min(5, delay - elapsed))
                    elapsed += 5

    except Exception as e:
        log.error(f"Batch video download error: {e}")
    finally:
        _batch_state["running"] = False
        _batch_state["current"] = ""


@app.post("/downloads/batch-videos")
async def batch_download_videos(
    min_delay: int = Form(300),
    max_delay: int = Form(1800),
):
    """Start downloading all pending videos with random delays between them."""
    if _batch_state["running"]:
        return RedirectResponse("/downloads?status=pending", status_code=303)

    _batch_state["running"] = True
    thread = threading.Thread(
        target=_batch_video_worker, args=(min_delay, max_delay), daemon=True,
    )
    thread.start()
    return RedirectResponse("/downloads?status=pending", status_code=303)


@app.post("/downloads/batch-stop")
async def batch_stop():
    """Stop the batch video download."""
    _batch_state["running"] = False
    return RedirectResponse("/downloads?status=pending", status_code=303)


@app.get("/api/downloads/batch-status")
async def batch_status():
    """Get the current batch download status."""
    return _batch_state


# ---------------------------------------------------------------------------
# Import queue (URL backfill)
# ---------------------------------------------------------------------------

# Patterns that identify Facebook post/content URLs
_FB_URL_RE = re.compile(
    r'https?://(?:www\.|m\.|mbasic\.)?facebook\.com/'
    r'(?:'
    r'[^/\s]+/posts/[\w.]+'            # /page/posts/id
    r'|permalink\.php\?[^\s"\'<>]+'     # permalink.php?story_fbid=...
    r'|[^/\s]+/photos/[^\s"\'<>]+'      # /page/photos/...
    r'|[^/\s]+/videos/[\w.]+'           # /page/videos/id
    r'|watch/[^\s"\'<>]+'               # /watch/?v=...
    r'|reel/[\w.]+'                      # /reel/id
    r'|share/[^\s"\'<>]+'               # /share/...
    r'|story\.php\?[^\s"\'<>]+'         # story.php?...
    r'|photo[./][^\s"\'<>]+'            # /photo/... or /photo.php?...
    r')',
    re.IGNORECASE,
)


def extract_fb_urls(raw_text: str) -> list[str]:
    """
    Extract and deduplicate Facebook post URLs from any raw text.

    Handles messy input: console logs, JSON, HTML, mixed prose, etc.
    Strips query params (except for permalink.php/story.php which need them),
    deduplicates, and returns clean URLs.
    """
    matches = _FB_URL_RE.findall(raw_text)

    cleaned = []
    seen = set()
    for url in matches:
        # Strip trailing punctuation/quotes that regex may have grabbed
        url = url.rstrip('",\'<>);]}\\ \t\n\r')

        # For permalink.php and story.php, keep query params (they contain the post ID)
        # For everything else, strip query params
        if 'permalink.php' not in url and 'story.php' not in url and 'photo.php' not in url:
            url = url.split('?')[0]

        # Normalize trailing slash
        url = url.rstrip('/')

        if url not in seen:
            seen.add(url)
            cleaned.append(url)

    return cleaned


@app.get("/import", response_class=HTMLResponse)
async def import_page(request: Request, status: str = "pending", message: str = "", message_type: str = ""):
    if status not in ("pending", "scraped", "failed", "duplicate"):
        status = "pending"
    items = db.get_import_queue(status=status)
    counts = db.get_import_counts()

    return templates.TemplateResponse("import.html", {
        "request": request,
        "items": items,
        "status": status,
        "counts": counts,
        "message": message,
        "message_type": message_type,
        "active_page": "import",
    })


@app.post("/import/add")
async def import_add_urls(
    urls: str = Form(""),
    page_name: str = Form(""),
):
    # Extract valid Facebook URLs from whatever the user pasted
    url_list = extract_fb_urls(urls)

    if not url_list:
        return RedirectResponse(
            "/import?message=No+Facebook+URLs+found+in+input&message_type=error",
            status_code=303,
        )

    added = db.add_import_urls(url_list, page_name=page_name)
    skipped = len(url_list) - added

    msg = f"Extracted+{len(url_list)}+URLs,+added+{added}+to+queue"
    if skipped:
        msg += f"+({skipped}+already+queued)"

    return RedirectResponse(f"/import?message={msg}&message_type=success", status_code=303)


@app.post("/import/{import_id}/delete")
async def import_delete(import_id: int):
    db.delete_import(import_id)
    return RedirectResponse("/import?status=pending", status_code=303)


@app.post("/import/{import_id}/retry")
async def import_retry(import_id: int):
    db.update_import_status(import_id, "pending")
    return RedirectResponse("/import?status=pending", status_code=303)


# ---------------------------------------------------------------------------
# Attachments (serve files)
# ---------------------------------------------------------------------------

@app.get("/attachment/{attachment_id}")
async def serve_attachment(attachment_id: int):
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM attachments WHERE id=?", (attachment_id,)
    ).fetchone()
    conn.close()

    if not row:
        return HTMLResponse("Not found", status_code=404)

    path = Path(row["local_path"])
    if not path.exists():
        return HTMLResponse("File not found on disk", status_code=404)

    return FileResponse(path)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@app.get("/api/stats")
async def api_stats():
    return db.get_stats()


@app.get("/api/posts")
async def api_posts(
    page_name: str = "",
    search: str = "",
    limit: int = 50,
    offset: int = 0,
):
    return db.get_posts(page_name=page_name, search=search, limit=limit, offset=offset)


@app.get("/api/posts/{post_id}")
async def api_post(post_id: str):
    post = db.get_post(post_id)
    if not post:
        return {"error": "not found"}
    post["comments"] = db.get_comments_for_post(post_id)
    post["attachments"] = db.get_attachments_for_post(post_id)
    post["people"] = db.get_people_for_post(post_id)
    return post


@app.get("/api/people")
async def api_people(search: str = ""):
    return db.get_people(search=search)


@app.get("/api/people/{person_id}")
async def api_person(person_id: int):
    person = db.get_person(person_id)
    if not person:
        return {"error": "not found"}
    person["pages"] = db.get_person_pages(person_id)
    person["posts"] = db.get_person_posts(person_id)
    person["comments"] = db.get_person_comments(person_id)
    person["entities"] = db.get_entities_for_person(person_id)
    return person


@app.get("/api/entities")
async def api_entities(search: str = ""):
    return db.get_entities(search=search)


@app.get("/api/entities/{entity_id}")
async def api_entity(entity_id: int):
    entity = db.get_entity(entity_id)
    if not entity:
        return {"error": "not found"}
    entity["pages"] = db.get_entity_pages(entity_id)
    entity["people"] = db.get_entity_people(entity_id)
    return entity


@app.post("/api/import")
async def api_import_urls(request: Request, page_name: str = Query("")):
    """
    Bulk import URLs via API.

    Accepts any raw text — console output, log files, JSON, HTML, plain URLs.
    Facebook post URLs are automatically extracted, cleaned, and deduplicated.

    Usage:
        curl -X POST http://localhost:8000/api/import --data-binary @urls.txt
        curl -X POST http://localhost:8000/api/import --data-binary @console_output.txt
        curl -X POST http://localhost:8000/api/import?page_name=MyPage --data-binary @urls.txt
        curl -X POST http://localhost:8000/api/import -H "Content-Type: application/json" \
             -d '{"urls": ["https://facebook.com/page/posts/123"], "page_name": "My Page"}'
    """
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    text = body.decode("utf-8", errors="ignore")

    if "application/json" in content_type:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        # If structured JSON with "urls" list, extract from each; otherwise treat whole blob as raw text
        if isinstance(data.get("urls"), list):
            raw = "\n".join(str(u) for u in data["urls"])
        else:
            raw = text
        page_name = data.get("page_name", page_name) if isinstance(data, dict) else page_name
    else:
        raw = text

    url_list = extract_fb_urls(raw)

    if not url_list:
        return JSONResponse({"error": "No Facebook URLs found in input"}, status_code=400)

    added = db.add_import_urls(url_list, page_name=page_name)
    skipped = len(url_list) - added

    return {
        "extracted": len(url_list),
        "added": added,
        "skipped": skipped,
        "urls": url_list,
    }


@app.post("/api/ingest")
async def api_ingest(request: Request):
    """
    Ingest full post data from the browser extension.

    Accepts structured JSON with posts, comments, image URLs, etc.
    Each post is saved directly to the database with all its comments.

    Expected payload:
    {
        "page_name": "Vote Kevin Crye",
        "page_url": "https://www.facebook.com/votekevincrye",
        "posts": [
            {
                "post_id": "abc123",
                "post_url": "https://...",
                "author": "...",
                "text": "...",
                "timestamp": "...",
                "image_urls": [...],
                "video_urls": [...],
                "reaction_count": "...",
                "comment_count_text": "...",
                "share_count_text": "...",
                "shared_from": null,
                "links": [...],
                "comments": [
                    {"author": "...", "text": "...", "timestamp": "...", "is_reply": false}
                ]
            }
        ]
    }
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    page_name = data.get("page_name", "")
    page_url = data.get("page_url", "")
    posts = data.get("posts", [])

    if not posts:
        return JSONResponse({"error": "No posts provided"}, status_code=400)

    # Load config for output dir
    config_path = Path(__file__).parent / "config.json"
    output_dir = Path("downloads")
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        output_dir = Path(config.get("output_dir", "downloads"))

    saved = 0
    skipped = 0
    total_comments = 0
    total_images_saved = 0
    total_videos_queued = 0

    for post in posts:
        post_id = post.get("post_id", "")
        if not post_id:
            continue

        # Check if already exists
        existing = db.get_post(post_id)
        if existing:
            skipped += 1
            continue

        # Create post directory for storing downloaded images
        page_key = re.sub(r'[^\w]', '_', page_name or "unknown")[:50]
        safe_id = re.sub(r'[^\w]', '_', post_id)[:50]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        post_dir = output_dir / page_key / f"{ts}_{safe_id}"

        # Build post data dict matching what db.save_post expects
        post_data = {
            "post_id": post_id,
            "page_name": page_name or post.get("author", "Unknown"),
            "page_url": page_url,
            "url": post.get("post_url", ""),
            "author": post.get("author", ""),
            "text": post.get("text", ""),
            "timestamp": post.get("timestamp", ""),
            "timestamp_raw": post.get("timestamp", ""),
            "shared_from": post.get("shared_from"),
            "shared_original_url": None,
            "links": post.get("links", []),
            "reaction_count": post.get("reaction_count", ""),
            "comment_count_text": post.get("comment_count_text", ""),
            "share_count_text": post.get("share_count_text", ""),
            "post_dir": str(post_dir),
        }

        db.save_post(post_data, account="extension")
        saved += 1

        # Save comments
        comments = post.get("comments", [])
        if comments:
            new_comments = db.save_comments(post_id, comments)
            total_comments += new_comments

        image_urls = post.get("image_urls", [])
        image_data = post.get("image_data", [])
        video_urls = post.get("video_urls", [])
        post_url_str = post.get("post_url", "")

        # Save images — prefer inline base64 data (captured from browser)
        if image_data:
            attachments_dir = post_dir / "attachments"
            attachments_dir.mkdir(parents=True, exist_ok=True)
            saved_paths = []
            for i, img in enumerate(image_data):
                try:
                    raw = base64.b64decode(img["data"])
                    ct = img.get("content_type", "image/jpeg")
                    ext = {
                        "image/jpeg": ".jpg", "image/png": ".png",
                        "image/gif": ".gif", "image/webp": ".webp",
                    }.get(ct, ".jpg")
                    filepath = attachments_dir / f"image_{i+1}{ext}"
                    filepath.write_bytes(raw)
                    saved_paths.append(str(filepath))
                except Exception as e:
                    log.warning(f"Failed to save inline image for {post_id}: {e}")
            if saved_paths:
                db.save_attachments(post_id, {"images": saved_paths, "videos": []})
                total_images_saved += len(saved_paths)
        elif image_urls:
            # Fallback: download from CDN URLs (may fail for private/expired)
            attachments_dir = post_dir / "attachments"
            try:
                downloaded = download_images(image_urls, attachments_dir)
                if downloaded:
                    db.save_attachments(post_id, {"images": downloaded, "videos": []})
                    total_images_saved += len(downloaded)
            except Exception as e:
                log.warning(f"Ingest image download failed for {post_id}: {e}")

        # Queue videos for gradual download (don't download now)
        if video_urls or any(p in post_url_str for p in ("/videos/", "/watch/", "/reel/")):
            try:
                db.queue_media_batch(
                    post_id, [], video_urls,
                    post_url=post_url_str,
                    account="extension",
                )
                total_videos_queued += len(video_urls)
            except Exception:
                pass

    return {
        "saved": saved,
        "skipped": skipped,
        "comments": total_comments,
        "images_saved": total_images_saved,
        "videos_queued": total_videos_queued,
        "total_submitted": len(posts),
    }


@app.get("/api/categories")
async def api_categories():
    return db.get_categories()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    db.init_db()


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="FB Monitor Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8000, help="Port")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    print(f"\nFB Monitor UI: http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
