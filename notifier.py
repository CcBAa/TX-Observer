"""
notifier.py — Discord Webhook & Telegram Bot push for TX-Observer.

Pipeline per job run:
  local .png  →  notify_all(target_type, image_path, message)
                      ├─ send_discord()   →  Discord channel (multipart upload)
                      └─ send_telegram()  →  Telegram topic  (sendPhoto upload)

Both platforms receive the image as a direct binary upload — no external
image host (Imgbb) required.  Discord and Telegram are isolated by
independent try/except blocks: a failure on one platform never blocks
the other.
"""

import logging
from pathlib import Path

import requests

import config

logger = logging.getLogger("tx_observer.notifier")

_REQUEST_TIMEOUT = 30   # seconds

# ---------------------------------------------------------------------------
# Target routing table — maps target_type to (discord_webhook, telegram_thread)
# ---------------------------------------------------------------------------

_ROUTING: dict[str, tuple[str, str]] = {
    "TSE": (config.DISCORD_WEBHOOK_TSE, config.TELEGRAM_THREAD_TSE),
    "OTC": (config.DISCORD_WEBHOOK_OTC, config.TELEGRAM_THREAD_OTC),
    "TX":  (config.DISCORD_WEBHOOK_TX,  config.TELEGRAM_THREAD_TX),
}


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def send_discord(webhook_url: str, image_path: Path, message: str) -> None:
    """
    Upload a local PNG and a text message to a Discord channel via Webhook.

    Parameters
    ----------
    webhook_url : Discord Webhook URL for the target channel.
    image_path  : Path to the local .png file to attach.
    message     : Caption / content text.

    Raises
    ------
    RuntimeError on HTTP error (status != 2xx).
    """
    image_path = Path(image_path)
    logger.info("Discord upload: %s → webhook", image_path.name)

    with image_path.open("rb") as fh:
        response = requests.post(
            webhook_url,
            data={"content": message},
            files={"file": (image_path.name, fh, "image/png")},
            timeout=_REQUEST_TIMEOUT,
        )

    if response.ok:
        logger.info("Discord upload OK (HTTP %d).", response.status_code)
    else:
        raise RuntimeError(
            f"Discord upload failed: HTTP {response.status_code} — {response.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(thread_id: str, image_path: Path, message: str) -> None:
    """
    Upload a local PNG and caption to a Telegram topic (supergroup thread).

    Parameters
    ----------
    thread_id  : Telegram message_thread_id (forum topic ID).
    image_path : Path to the local .png file to send.
    message    : Caption text (≤ 1024 chars for sendPhoto).

    Raises
    ------
    RuntimeError on HTTP error or Telegram API error response.
    """
    image_path = Path(image_path)
    endpoint = (
        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
    )
    logger.info("Telegram upload: %s → thread %s", image_path.name, thread_id)

    with image_path.open("rb") as fh:
        response = requests.post(
            endpoint,
            data={
                "chat_id":           config.TELEGRAM_CHAT_ID,
                "message_thread_id": thread_id,
                "caption":           message[:1024],
            },
            files={"photo": (image_path.name, fh, "image/png")},
            timeout=_REQUEST_TIMEOUT,
        )

    if response.ok:
        logger.info("Telegram upload OK (HTTP %d).", response.status_code)
    else:
        raise RuntimeError(
            f"Telegram upload failed: HTTP {response.status_code} — {response.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Public routing entry point
# ---------------------------------------------------------------------------

def notify_all(target_type: str, image_path: "str | Path", message: str) -> None:
    """
    Route a chart notification to both Discord and Telegram.

    Discord and Telegram are wrapped in independent try/except blocks so a
    failure on one platform never blocks the other.  All errors are logged
    with the HTTP status code and response body for quick diagnosis.

    Parameters
    ----------
    target_type : One of 'TSE', 'OTC', 'TX'.
    image_path  : Path to the local .png chart file.
    message     : Text caption / announcement body.

    Raises
    ------
    ValueError  If target_type is not one of the three recognised values.
    """
    if target_type not in _ROUTING:
        raise ValueError(
            f"notify_all: unknown target_type {target_type!r}. "
            f"Must be one of: {list(_ROUTING)}"
        )

    image_path = Path(image_path)
    discord_webhook, telegram_thread = _ROUTING[target_type]

    # ── Discord ──────────────────────────────────────────────────────────────
    try:
        send_discord(discord_webhook, image_path, message)
    except Exception as exc:
        logger.error(
            "[%s] Discord push failed: %s", target_type, exc
        )

    # ── Telegram ─────────────────────────────────────────────────────────────
    try:
        send_telegram(telegram_thread, image_path, message)
    except Exception as exc:
        logger.error(
            "[%s] Telegram push failed: %s", target_type, exc
        )
