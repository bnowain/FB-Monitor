"""
screenshot_parser.py — Extract post/comment data from Facebook screenshots.

Uses Claude's vision API to analyze screenshots and return structured data
that can be saved to the database. Designed for manual backfill: take
screenshots while browsing logged in, then parse them offline.
"""

import base64
import json
import logging
import mimetypes
from pathlib import Path

log = logging.getLogger("fb-monitor")

EXTRACTION_PROMPT = """\
Analyze this Facebook screenshot and extract all visible post and comment data.

Return a JSON object with this exact structure:
{
  "posts": [
    {
      "author": "Name of the person/page who posted",
      "text": "Full post text (preserve line breaks)",
      "timestamp": "Timestamp as shown (e.g. 'March 5 at 2:30 PM', '2h', 'Yesterday')",
      "shared_from": "Original author if this is a shared post, or null",
      "reaction_count": "Reaction count as shown (e.g. '42', '1.2K') or null",
      "comment_count": "Comment count as shown or null",
      "share_count": "Share count as shown or null",
      "links": ["any URLs visible in the post text"],
      "comments": [
        {
          "author": "Commenter name",
          "text": "Comment text",
          "timestamp": "Comment timestamp as shown",
          "is_reply": false
        }
      ]
    }
  ],
  "page_name": "Name of the Facebook page if visible, or null"
}

Rules:
- Extract ALL visible posts. There may be one or several in the screenshot.
- Extract ALL visible comments and replies for each post.
- Mark replies (indented comments responding to other comments) with is_reply: true.
- Preserve the exact text content — don't summarize or paraphrase.
- If text is cut off, include what's visible and add "..." at the end.
- For timestamps, use exactly what's shown on screen.
- If a field is not visible, use null.
- Return ONLY the JSON object, no markdown fencing or explanation.
"""


def encode_image(image_path: str) -> tuple[str, str]:
    """Read an image file and return (base64_data, media_type)."""
    path = Path(image_path)
    data = path.read_bytes()
    b64 = base64.standard_b64encode(data).decode("utf-8")

    media_type = mimetypes.guess_type(str(path))[0] or "image/png"
    return b64, media_type


def encode_image_bytes(image_bytes: bytes, filename: str = "screenshot.png") -> tuple[str, str]:
    """Encode raw image bytes to base64 with media type."""
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    media_type = mimetypes.guess_type(filename)[0] or "image/png"
    return b64, media_type


def parse_screenshot(
    image_b64: str,
    media_type: str,
    api_key: str,
    model: str = "claude-sonnet-4-20250514",
    page_name: str = "",
) -> dict:
    """
    Send a screenshot to Claude's vision API and extract structured data.

    Args:
        image_b64: Base64-encoded image data
        media_type: MIME type (e.g. 'image/png')
        api_key: Anthropic API key
        model: Claude model to use
        page_name: Optional page name hint for context

    Returns:
        Parsed data dict with 'posts' list, or {'error': '...'} on failure.
    """
    import httpx

    prompt = EXTRACTION_PROMPT
    if page_name:
        prompt += f"\n\nContext: This screenshot is from the Facebook page '{page_name}'."

    payload = {
        "model": model,
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
    }

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=60.0,
        )

        if resp.status_code != 200:
            error_text = resp.text[:500]
            log.error(f"Anthropic API error {resp.status_code}: {error_text}")
            return {"error": f"API error {resp.status_code}: {error_text}"}

        result = resp.json()
        text_content = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text_content += block["text"]

        # Parse the JSON response — strip markdown fencing if present
        text_content = text_content.strip()
        if text_content.startswith("```"):
            # Remove ```json ... ``` wrapper
            lines = text_content.split("\n")
            text_content = "\n".join(lines[1:])
            if text_content.endswith("```"):
                text_content = text_content[:-3]
            text_content = text_content.strip()

        parsed = json.loads(text_content)
        return parsed

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse API response as JSON: {e}")
        return {"error": f"Failed to parse response: {e}", "raw_response": text_content}
    except httpx.TimeoutException:
        return {"error": "API request timed out (60s)"}
    except Exception as e:
        log.error(f"Screenshot parsing failed: {e}")
        return {"error": str(e)}


def parse_multiple_screenshots(
    images: list[tuple[str, str]],
    api_key: str,
    model: str = "claude-sonnet-4-20250514",
    page_name: str = "",
) -> dict:
    """
    Send multiple screenshots in a single API call for batch processing.

    Args:
        images: List of (base64_data, media_type) tuples
        api_key: Anthropic API key
        model: Claude model to use
        page_name: Optional page name hint

    Returns:
        Parsed data dict with 'posts' list from all screenshots combined.
    """
    import httpx

    prompt = EXTRACTION_PROMPT + (
        "\n\nThese are multiple screenshots from the same page. "
        "They may show the same post across screenshots (e.g. post text in one, "
        "comments in another). Merge data for the same post if you can identify it. "
        "Deduplicate comments that appear in multiple screenshots."
    )
    if page_name:
        prompt += f"\n\nContext: These screenshots are from the Facebook page '{page_name}'."

    content = []
    for b64, media_type in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64,
            },
        })
    content.append({"type": "text", "text": prompt})

    payload = {
        "model": model,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": content}],
    }

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=120.0,
        )

        if resp.status_code != 200:
            error_text = resp.text[:500]
            return {"error": f"API error {resp.status_code}: {error_text}"}

        result = resp.json()
        text_content = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text_content += block["text"]

        text_content = text_content.strip()
        if text_content.startswith("```"):
            lines = text_content.split("\n")
            text_content = "\n".join(lines[1:])
            if text_content.endswith("```"):
                text_content = text_content[:-3]
            text_content = text_content.strip()

        return json.loads(text_content)

    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse response: {e}", "raw_response": text_content}
    except httpx.TimeoutException:
        return {"error": "API request timed out (120s)"}
    except Exception as e:
        return {"error": str(e)}
