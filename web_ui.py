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
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database as db

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
    offset: int = Query(0),
):
    posts = db.get_posts(page_name=page_name, search=search, offset=offset)
    page_names = db.get_page_names()

    # Enrich each post with attachment counts and comment count
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
    conn.close()

    return templates.TemplateResponse("posts.html", {
        "request": request,
        "posts": posts,
        "page_names": page_names,
        "page_name": page_name,
        "search": search,
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

    return templates.TemplateResponse("post_detail.html", {
        "request": request,
        "post": post,
        "comments": comments,
        "attachments": attachments,
        "links": links,
        "linked_people": linked_people,
        "all_people": all_people,
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

    return templates.TemplateResponse("person_detail.html", {
        "request": request,
        "person": person,
        "pages": pages,
        "posts": posts,
        "comments": comments,
        "all_page_names": all_page_names,
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
    return person


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
