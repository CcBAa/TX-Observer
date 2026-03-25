"""
notifier.py — Image hosting (Imgbb) and LINE Messaging API push for TX-Observer.

Pipeline per job run:
  local .png  →  upload_to_imgbb()  →  HTTPS URL
                                            │
                                            ▼
                                send_push_message()  →  LINE group / user

Billing note:
  LINE Messaging API counts one Push call as one "message unit" regardless
  of how many objects are in the 'messages' array.  Packing the text summary
  and the combined chart image into a single push call costs 1 unit.
"""

import base64
import logging
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("tx_observer.notifier")

_IMGBB_UPLOAD_URL  = "https://api.imgbb.com/1/upload"
_LINE_PUSH_URL     = "https://api.line.me/v2/bot/message/push"
_REQUEST_TIMEOUT   = 30
_LINE_TEXT_MAX_LEN = 5_000
_IMGBB_MAX_RETRIES = 3
_IMGBB_RETRY_DELAY = 5   # seconds between Imgbb upload retry attempts


# ---------------------------------------------------------------------------
# Imgbb upload
# ---------------------------------------------------------------------------

def upload_to_imgbb(image_path: Path, api_key: str) -> str:
    """
    Upload a local PNG file to Imgbb and return the public HTTPS URL.

    Parameters
    ----------
    image_path : Path to the local .png file.
    api_key    : Imgbb API key.

    Returns
    -------
    str — Public HTTPS direct image URL (data.url).

    Raises
    ------
    FileNotFoundError  If the file does not exist.
    RuntimeError       On HTTP error or malformed response.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")

    logger.info("Uploading %s to Imgbb...", image_path.name)

    with image_path.open("rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    last_err: str = ""
    for attempt in range(1, _IMGBB_MAX_RETRIES + 1):
        try:
            response = requests.post(
                _IMGBB_UPLOAD_URL,
                params={"key": api_key},
                data={"image": image_b64},
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            last_err = str(exc)
            logger.warning(
                "Imgbb upload attempt %d/%d failed (%s): %s",
                attempt, _IMGBB_MAX_RETRIES, image_path.name, last_err,
            )
            if attempt < _IMGBB_MAX_RETRIES:
                time.sleep(_IMGBB_RETRY_DELAY)
            continue

        if response.status_code != 200:
            last_err = f"HTTP {response.status_code} — {response.text[:200]}"
            logger.warning(
                "Imgbb upload attempt %d/%d HTTP error (%s): %s",
                attempt, _IMGBB_MAX_RETRIES, image_path.name, last_err,
            )
            if attempt < _IMGBB_MAX_RETRIES:
                time.sleep(_IMGBB_RETRY_DELAY)
            continue

        try:
            url: str = response.json()["data"]["url"]
        except (KeyError, ValueError):
            last_err = f"malformed response: {response.text[:200]}"
            logger.warning(
                "Imgbb upload attempt %d/%d bad response (%s): %s",
                attempt, _IMGBB_MAX_RETRIES, image_path.name, last_err,
            )
            if attempt < _IMGBB_MAX_RETRIES:
                time.sleep(_IMGBB_RETRY_DELAY)
            continue

        logger.info(
            "Imgbb upload OK (attempt %d/%d): %s → %s",
            attempt, _IMGBB_MAX_RETRIES, image_path.name, url,
        )
        return url

    # All retries exhausted — log a short warning and raise so caller can handle
    logger.warning(
        "Imgbb upload failed after %d attempts for %s: %s",
        _IMGBB_MAX_RETRIES, image_path.name, last_err,
    )
    raise RuntimeError(
        f"Imgbb upload failed after {_IMGBB_MAX_RETRIES} attempts "
        f"({image_path.name}): {last_err}"
    )


# ---------------------------------------------------------------------------
# LINE Messaging API — Push Message
# ---------------------------------------------------------------------------

def send_push_message(
    channel_access_token: str,
    target_id:            str,
    text:                 str,
    image_url:            Optional[str] = None,
) -> bool:
    """
    Send a LINE Messaging API Push Message (text + optional image).

    Bundles text summary and the combined 5K+60K chart into a single
    push call (= 1 billing unit).

    Parameters
    ----------
    channel_access_token : LINE Channel Access Token.
    target_id            : Destination Group ID (C…) or User ID (U…).
    text                 : Market summary string.
    image_url            : Public HTTPS URL of the combined chart on Imgbb.

    Returns
    -------
    bool — True if LINE API returned HTTP 200, False otherwise.
    """
    if not channel_access_token:
        logger.error("LINE_CHANNEL_ACCESS_TOKEN is empty — push aborted.")
        return False
    if not target_id:
        logger.error("LINE_TARGET_ID is empty — push aborted.")
        return False

    messages: list[dict] = [{"type": "text", "text": _truncate(text)}]

    if image_url:
        messages.append({
            "type":               "image",
            "originalContentUrl": image_url,
            "previewImageUrl":    image_url,
        })
    else:
        logger.warning("No image URL supplied — text-only push.")

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
            len(messages), target_id,
        )
        return True

    try:
        err_body = response.json()
        err_msg  = err_body.get("message", response.text[:300])
    except ValueError:
        err_msg = response.text[:300]

    logger.error(
        "LINE Push failed: HTTP %d — %s", response.status_code, err_msg
    )
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_len: int = _LINE_TEXT_MAX_LEN) -> str:
    return text[:max_len] if len(text) > max_len else text
