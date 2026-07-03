"""
src/instagram_publisher.py
Publishes to Instagram via the Graph API.

Single image  → /media (image) → /media_publish
Carousel      → N × /media (carousel_item) → /media (CAROUSEL) → /media_publish

Progress is written to src.progress so the Streamlit UI can render
a live step-by-step indicator while this runs in a background thread.
"""

import os
import time
import requests
from typing import Any

import src.progress as progress

NS            = "ig"   # progress namespace (matches ui/instagram_ui.py)
GRAPH_BASE    = "https://graph.instagram.com/v25.0"
POLL_INTERVAL = 3    # seconds between status-check polls
POLL_TIMEOUT  = 120  # seconds before giving up on a container


# ── Credentials ───────────────────────────────────────────────────────────────
def _creds() -> dict[str, str]:
    token   = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    user_id = os.getenv("INSTAGRAM_USER_ID", "")
    if not token or not user_id:
        raise EnvironmentError(
            "INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID must be set in .env"
        )
    return {"access_token": token, "ig_user_id": user_id}


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _format_api_error(error: Any) -> str:
    """Extract a readable message from a Graph API error object."""
    if isinstance(error, dict):
        msg   = error.get("message", "")
        code  = error.get("code", "")
        trace = error.get("fbtrace_id", "")
        parts = [f"code {code}" if code else "", msg, f"(trace: {trace})" if trace else ""]
        return " — ".join(p for p in parts if p) or str(error)
    return str(error)


def _post(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """POST to Graph API.
    Sends the access_token as a Bearer Authorization header and all other
    fields in the POST body (application/x-www-form-urlencoded).
    This avoids URL-encoding edge cases and matches Meta's preferred pattern
    for the v25.0 API.
    """
    token = params.pop("access_token", "")
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(endpoint, data=params, headers=headers, timeout=30)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Graph API error: {_format_api_error(data['error'])}")
    resp.raise_for_status()
    return data


def _get(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET from Graph API using Bearer Authorization header."""
    token = params.pop("access_token", "")
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(endpoint, params=params, headers=headers, timeout=30)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Graph API error: {_format_api_error(data['error'])}")
    resp.raise_for_status()
    return data


def _poll_until_ready(
    container_id: str,
    access_token: str,
    step_index:   int,
    step_label:   str,
) -> None:
    """
    Block until the container reports FINISHED.
    Updates the progress step with elapsed time while waiting.
    """
    deadline   = time.time() + POLL_TIMEOUT
    start_time = time.time()
    while time.time() < deadline:
        data   = _get(
            f"{GRAPH_BASE}/{container_id}",
            {"fields": "status_code", "access_token": access_token},
        )
        status = data.get("status_code", "IN_PROGRESS")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"Instagram rejected container {container_id}.")
        elapsed = int(time.time() - start_time)
        progress.update(NS, step_index, "active", f"{step_label} — processing… ({elapsed}s)")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Container {container_id} did not finish within {POLL_TIMEOUT}s.")


# ── Container creation ────────────────────────────────────────────────────────
def _create_image_container(
    ig_user_id:       str,
    access_token:     str,
    image_url:        str,
    caption:          str | None = None,
    alt_text:         str | None = None,
    is_carousel_item: bool       = False,
) -> str:
    params: dict[str, Any] = {"image_url": image_url, "access_token": access_token}
    if is_carousel_item:
        params["is_carousel_item"] = "true"
    elif caption:
        params["caption"] = caption

    if alt_text:
        params["accessibility_caption"] = alt_text

    data = _post(f"{GRAPH_BASE}/{ig_user_id}/media", params)
    return data["id"]


def _create_carousel_container(
    ig_user_id:   str,
    access_token: str,
    children:     list[str],
    caption:      str,
) -> str:
    params = {
        "media_type":   "CAROUSEL",
        "children":     ",".join(children),
        "caption":      caption,
        "access_token": access_token,
    }
    data = _post(f"{GRAPH_BASE}/{ig_user_id}/media", params)
    return data["id"]


def _publish_container(ig_user_id: str, access_token: str, container_id: str) -> str:
    data = _post(
        f"{GRAPH_BASE}/{ig_user_id}/media_publish",
        {"creation_id": container_id, "access_token": access_token},
    )
    return data["id"]


# ── Public API ────────────────────────────────────────────────────────────────
def publish_to_instagram(
    image_urls: list[str],
    caption:    str,
    alt_texts:  list[str] | None = None,
) -> dict[str, Any]:
    """
    Upload and publish images to Instagram.
    alt_texts: list of strings matching image_urls length.
    """
    if not image_urls:
        raise ValueError("At least one image URL is required.")
    if len(image_urls) > 10:
        raise ValueError("Instagram allows a maximum of 10 carousel items.")

    n       = len(image_urls)
    is_carousel = n > 1

    # ── Build step labels up front so the UI renders them immediately ─────────
    if is_carousel:
        step_labels = (
            [f"Upload image {i+1} of {n}"   for i in range(n)]
            + ["Create carousel container"]
            + ["Publish carousel"]
        )
    else:
        step_labels = ["Upload image", "Process image", "Publish"]

    progress.start(NS, step_labels)

    try:
        c            = _creds()
        ig_user_id   = c["ig_user_id"]
        access_token = c["access_token"]

        # ── Single image ──────────────────────────────────────────────────────
        if not is_carousel:
            # Step 0 — upload
            progress.update(NS, 0, "active", "Sending image to Instagram…")
            container_id = _create_image_container(
                ig_user_id, access_token, image_urls[0],
                caption=caption,
                alt_text=alt_texts[0] if alt_texts else None
            )
            progress.update(NS, 0, "done", f"Container ID: {container_id[:12]}…")

            # Step 1 — process
            progress.update(NS, 1, "active", "Waiting for Instagram to process…")
            _poll_until_ready(container_id, access_token, 1, "Processing")
            progress.update(NS, 1, "done", "Ready to publish")

            # Step 2 — publish
            progress.update(NS, 2, "active", "Publishing…")
            post_id = _publish_container(ig_user_id, access_token, container_id)
            progress.update(NS, 2, "done", f"Post ID: {post_id}")

            result = {"success": True, "post_id": post_id, "type": "single"}
            progress.finish(NS, result)
            return result

        # ── Carousel ──────────────────────────────────────────────────────────
        child_ids: list[str] = []
        for i, url in enumerate(image_urls):
            step_i = i
            progress.update(NS, step_i, "active", f"Sending to Instagram API…")
            alt = alt_texts[i] if (alt_texts and i < len(alt_texts)) else None
            cid = _create_image_container(
                ig_user_id, access_token, url,
                alt_text=alt,
                is_carousel_item=True
            )
            progress.update(NS, step_i, "active", f"Processing container {cid[:10]}…")
            _poll_until_ready(cid, access_token, step_i, f"Image {i+1}")
            progress.update(NS, step_i, "done", f"Ready ✓  ({cid[:10]}…)")
            child_ids.append(cid)

        # Carousel container step
        carousel_step = n
        progress.update(NS, carousel_step, "active", "Assembling carousel…")
        carousel_id = _create_carousel_container(
            ig_user_id, access_token, child_ids, caption
        )
        _poll_until_ready(carousel_id, access_token, carousel_step, "Carousel container")
        progress.update(NS, carousel_step, "done", f"Container ready ({carousel_id[:10]}…)")

        # Publish step
        pub_step = n + 1
        progress.update(NS, pub_step, "active", "Publishing carousel to Instagram…")
        post_id = _publish_container(ig_user_id, access_token, carousel_id)
        progress.update(NS, pub_step, "done", f"Post ID: {post_id}")

        result = {"success": True, "post_id": post_id, "type": "carousel", "items": n}
        progress.finish(NS, result)
        return result

    except Exception as exc:
        # Mark every still-active or pending step as error
        state = progress.read(NS)
        for idx, step in enumerate(state.get("steps", [])):
            if step["status"] in ("active", "pending"):
                progress.update(NS, idx, "error", str(exc)[:120])
        progress.fail(NS, str(exc))
        return {"success": False, "error": str(exc)}
