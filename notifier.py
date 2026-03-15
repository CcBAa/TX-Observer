"""
notifier.py — Image hosting (Imgbb) and LINE Messaging API push for TX-Observer.

Pipeline per job run:
  local .png  →  upload_to_imgbb()  →  HTTPS URL
                                            │
                                            ▼
                                send_push_message()  →  LINE group / user

Why this two-step design:
  LINE Messaging API's image message type only accepts a publicly reachable
  HTTPS URL (originalContentUrl / previewImageUrl).  Imgbb provides free
  image hosting via a simple API-key POST request, which is all we need to
  bridge local files to LINE without an S3 bucket or VPS web server.

Billing note:
  LINE Messaging API counts one Push call as one "message unit" regardless
  of how many objects are inside the 'messages' array.  Packing the text
  summary and both chart images into a single push call therefore costs
  1 unit, not 3.  With ~152 push calls/month this stays well under the
  200-unit free-tier limit.
"""

import base64
import logging
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("tx_observer.notifier")

_IMGBB_UPLOAD_URL  = "https://api.imgbb.com/1/upload"
_LINE_PUSH_URL     = "https://api.line.me/v2/bot/message/push"
_REQUEST_TIMEOUT   = 30   # seconds
_LINE_TEXT_MAX_LEN = 5_000


# ---------------------------------------------------------------------------
# Imgbb upload
# ---------------------------------------------------------------------------

def upload_to_imgbb(image_path: Path, api_key: str) -> str:
    """
    Upload a local PNG file to Imgbb and return the public HTTPS URL.

    Authentication uses an API key passed as a query parameter — no OAuth
    or account login is required.

    Parameters
    ----------
    image_path:
        Path to the local .png file to upload.
    api_key:
        Imgbb API key obtained from https://api.imgbb.com/.

    Returns
    -------
    str
        Public HTTPS direct image URL from ``data.url``,
        e.g. ``https://i.ibb.co/AbCdEfG/filename.png``.

    Raises
    ------
    FileNotFoundError
        If *image_path* does not exist.
    RuntimeError
        If the Imgbb API returns a non-200 status, the response is malformed,
        or a network error occurs.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")

    logger.info("Uploading %s to Imgbb...", image_path.name)

    with image_path.open("rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    # API key goes in the query string; image base64 in the POST body
    params  = {"key": api_key}
    payload = {"image": image_b64}

    try:
        response = requests.post(
            _IMGBB_UPLOAD_URL,
            params=params,
            data=payload,
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Imgbb upload timed out after {_REQUEST_TIMEOUT}s for {image_path.name}")
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"Imgbb upload connection error for {image_path.name}: {exc}")

    if response.status_code != 200:
        raise RuntimeError(
            f"Imgbb upload failed for {image_path.name}: "
            f"HTTP {response.status_code} — {response.text[:300]}"
        )

    try:
        url: str = response.json()["data"]["url"]
    except (KeyError, ValueError) as exc:
        raise RuntimeError(
            f"Imgbb response missing 'data.url' for {image_path.name}: {response.text[:300]}"
        ) from exc

    logger.info("Imgbb upload OK: %s → %s", image_path.name, url)
    return url


# ---------------------------------------------------------------------------
# LINE Messaging API — Push Message
# ---------------------------------------------------------------------------

def send_push_message(
    channel_access_token: str,
    target_id: str,
    text: str,
    image_url_60k: Optional[str] = None,
    image_url_5k:  Optional[str] = None,
) -> bool:
    """
    Send a LINE Messaging API Push Message to a group or user.

    The payload bundles up to three message objects into a single push call:
      [0] text   — market summary
      [1] image  — 60K chart (if *image_url_60k* is provided)
      [2] image  — 5K chart  (if *image_url_5k* is provided)

    Packing everything into one push call means it is billed as **1 message
    unit**, not 3, keeping monthly usage well within the 200-unit free tier.

    Parameters
    ----------
    channel_access_token:
        LINE Channel Access Token (long-lived stateless token).
    target_id:
        Destination LINE Group ID (``C…``) or User ID (``U…``).
    text:
        Market summary string (truncated to 5 000 chars if necessary).
    image_url_60k:
        Public HTTPS URL of the 60-minute chart on Imgbb.  Pass ``None`` to
        omit the 60K image from this push.
    image_url_5k:
        Public HTTPS URL of the 5-minute chart on Imgbb.  Pass ``None`` to
        omit the 5K image from this push.

    Returns
    -------
    bool
        ``True`` if the LINE API returned HTTP 200, ``False`` otherwise.
        Failures are logged but do not raise, allowing the scheduler to
        continue to the next job without crashing.
    """
    if not channel_access_token:
        logger.error("LINE_CHANNEL_ACCESS_TOKEN is empty — push message aborted.")
        return False
    if not target_id:
        logger.error("LINE_TARGET_ID is empty — push message aborted.")
        return False

    # Build messages array
    messages: list[dict] = [{"type": "text", "text": _truncate(text)}]

    for label, url in (("60K", image_url_60k), ("5K", image_url_5k)):
        if url:
            messages.append(
                {
                    "type": "image",
                    "originalContentUrl": url,
                    "previewImageUrl":    url,
                }
            )
        else:
            logger.warning("No %s image URL supplied — image omitted from push.", label)

    payload = {"to": target_id, "messages": messages}
    headers = {
        "Authorization": f"Bearer {channel_access_token}",
        "Content-Type":  "application/json",
    }

    try:
        response = requests.post(
            _LINE_PUSH_URL,
            headers=headers,
            json=payload,
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        logger.error("LINE Push timed out after %ds.", _REQUEST_TIMEOUT)
        return False
    except requests.exceptions.ConnectionError as exc:
        logger.error("LINE Push connection error: %s", exc)
        return False
    except Exception as exc:
        logger.error("LINE Push unexpected error: %s", exc, exc_info=True)
        return False

    if response.status_code == 200:
        logger.info(
            "LINE Push OK — %d message object(s) sent to %s.",
            len(messages),
            target_id,
        )
        return True

    # Structured error logging: surface the LINE API error message if present
    try:
        err_body = response.json()
        err_msg  = err_body.get("message", response.text[:300])
    except ValueError:
        err_msg = response.text[:300]

    logger.error(
        "LINE Push failed: HTTP %d — %s",
        response.status_code,
        err_msg,
    )
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_len: int = _LINE_TEXT_MAX_LEN) -> str:
    """Truncate *text* to LINE Messaging API's per-message character limit."""
    return text[:max_len] if len(text) > max_len else text
