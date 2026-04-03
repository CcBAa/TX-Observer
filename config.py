"""
config.py — Configuration, security, and logging setup for TX-Observer.

Responsibilities:
  - Load environment variables from .env via python-dotenv
  - Validate all required credentials at startup (ValueError on missing vars)
  - Configure the root logger to write to both stdout and app.log
  - Expose Discord / Telegram credentials as module-level constants
"""

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 全域時區硬化 — 強制整個 process 使用 Asia/Taipei (UTC+8)
# ---------------------------------------------------------------------------
os.environ["TZ"] = "Asia/Taipei"
if hasattr(time, "tzset"):
    time.tzset()

_TW_TZ = pytz.timezone("Asia/Taipei")

# ---------------------------------------------------------------------------
# Load .env file (safe to call even if the file doesn't exist)
# ---------------------------------------------------------------------------
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s CST [%(levelname)-8s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _TaipeiFormatter(logging.Formatter):
    """
    Log formatter that always stamps records in Asia/Taipei time (UTC+8).

    Python 預設的 logging.Formatter 用 time.localtime()，在 UTC 伺服器上
    會輸出 UTC 時間戳。此 formatter 覆寫 formatTime()，強制轉換為台北時間，
    確保 app.log 與終端機顯示的時間戳與台灣交易時間一致。
    """

    def formatTime(self, record: logging.LogRecord, datefmt: "str | None" = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=_TW_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime(_DATE_FORMAT)


_DEFAULT_LOG_FILE = str(Path(__file__).parent / "app.log")


def setup_logging(log_file: str = _DEFAULT_LOG_FILE) -> logging.Logger:
    """
    Configure the root logger with two handlers:
      1. StreamHandler  → stdout (visible in the terminal / systemd journal)
      2. FileHandler    → app.log (persistent record for headless server debugging)

    All timestamps are formatted in Asia/Taipei time (UTC+8) via _TaipeiFormatter,
    regardless of the server's system timezone.

    Call this exactly once in main.py before importing any other project module.

    Returns the 'tx_observer' logger that all sub-modules inherit from.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Avoid adding duplicate handlers if setup_logging() is called more than once
    if root_logger.handlers:
        return logging.getLogger("tx_observer")

    formatter = _TaipeiFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # Console handler — INFO+ 至終端機
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    # File handler — DEBUG+ 至 app.log
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Silence noisy third-party loggers
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)

    return logging.getLogger("tx_observer")


# ---------------------------------------------------------------------------
# Credential validation & module-level constants
# ---------------------------------------------------------------------------

_REQUIRED_VARS = (
    # Discord Webhooks
    "DISCORD_WEBHOOK_TSE",
    "DISCORD_WEBHOOK_OTC",
    "DISCORD_WEBHOOK_TX",
    # Telegram Bot
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_THREAD_TSE",
    "TELEGRAM_THREAD_OTC",
    "TELEGRAM_THREAD_TX",
)


def _load_and_validate() -> dict[str, str]:
    """
    Read and validate all required credentials from the environment.

    Returns a dict keyed by variable name.

    Raises:
        ValueError: If any required variable is missing or empty,
                    halting the program with a clear diagnostic message.
    """
    creds = {key: os.getenv(key, "").strip() for key in _REQUIRED_VARS}
    missing = [key for key, val in creds.items() if not val]

    if missing:
        bullet_list = "\n".join(f"    • {k}" for k in missing)
        raise ValueError(
            "\n"
            "  [TX-Observer] FATAL: The following environment variables are not configured:\n"
            f"{bullet_list}\n"
            "  Steps to fix:\n"
            "    1. Copy .env.example  →  .env\n"
            "    2. Fill in every value in .env\n"
            "    3. Restart TX-Observer\n"
        )

    return creds


# Validate at import time — process terminates immediately if any var is missing.
_creds = _load_and_validate()

# Discord Webhooks
DISCORD_WEBHOOK_TSE: str = _creds["DISCORD_WEBHOOK_TSE"]
DISCORD_WEBHOOK_OTC: str = _creds["DISCORD_WEBHOOK_OTC"]
DISCORD_WEBHOOK_TX:  str = _creds["DISCORD_WEBHOOK_TX"]

# Telegram Bot
TELEGRAM_BOT_TOKEN:  str = _creds["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID:    str = _creds["TELEGRAM_CHAT_ID"]
TELEGRAM_THREAD_TSE: str = _creds["TELEGRAM_THREAD_TSE"]
TELEGRAM_THREAD_OTC: str = _creds["TELEGRAM_THREAD_OTC"]
TELEGRAM_THREAD_TX:  str = _creds["TELEGRAM_THREAD_TX"]
