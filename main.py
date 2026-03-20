"""
main.py — Entry point and scheduler for TX-Observer.

Supported symbols
-----------------
  TXFR1   : 台指期貨近一連續合約  (futures, day + night session)
  TSE/001 : 加權指數              (spot, Mon–Fri 09:00–13:30)
  OTC/101 : 櫃買指數              (spot, Mon–Fri 09:00–13:30)

Scheduler design — trigger times (Asia/Taipei)
-----------------------------------------------
  ┌────────────────┬──────────────────────────────────────────────────────┐
  │  Futures (TXF) │  Day : 08:45 09:45 10:45 11:45 12:45 13:45          │
  │                │  Night (early) Mon–Fri : 15:00 … 23:00 (every hour) │
  │                │  Night (late)  Tue–Sat : 00:00 … 05:00 (every hour) │
  ├────────────────┼──────────────────────────────────────────────────────┤
  │  Spot (TSE/OTC)│  09:00 10:00 11:00 12:00 13:00                      │
  └────────────────┴──────────────────────────────────────────────────────┘

Error isolation
---------------
  If one symbol fails (fetch / render / upload / push), the error is logged
  and execution continues to the next symbol without crashing the scheduler.

Shioaji login
-------------
  The API singleton is initialized once at startup via fetcher.init_api().
  Individual job calls reuse the cached singleton — no re-login per task.
"""

import argparse
import logging
import sys
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Optional

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config import get_credentials, setup_logging

logger = setup_logging()

from fetcher import ShioajiDataFetcher, init_api   # noqa: E402
from notifier import send_push_message, upload_to_imgbb  # noqa: E402
from renderer import render_combined_chart           # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TW_TZ      = pytz.timezone("Asia/Taipei")
CHARTS_DIR = Path("charts")

_BARS_5K  = 200   # 1-min → 5K: need extra history for MA60
_BARS_60K = 100   # 1-min → 60K: ~5 trading days

# Symbols dispatched in each job
_FUTURES_SYMBOLS: list[tuple[str, str]] = [
    ("TXFR1", "台指期近一"),
]
_SPOT_SYMBOLS: list[tuple[str, str]] = [
    ("TSE/001", "加權指數"),
    ("OTC/101", "櫃買指數"),
]


# ---------------------------------------------------------------------------
# Trading-hours safety gate
# ---------------------------------------------------------------------------

def is_trading_time(dt: datetime, market: str = "futures") -> bool:
    """
    Return True if *dt* (UTC+8, timezone-aware) is within trading hours
    for the given market.

    market="futures" — TXF: day 08:45–13:45, night 15:00–next-day 05:00
    market="spot"    — TSE/OTC: Mon–Fri 09:00–13:30 only
    """
    weekday = dt.weekday()   # 0 = Monday … 6 = Sunday
    t: dtime = dt.time()

    if market == "spot":
        # TSE/OTC indices: weekdays only, 09:00–13:30
        if weekday in (5, 6):
            return False
        return dtime(9, 0) <= t <= dtime(13, 30)

    # Futures (TXF)
    _NIGHT_END   = dtime(5, 0)
    _DAY_START   = dtime(8, 45)
    _DAY_END     = dtime(13, 45)
    _NIGHT_START = dtime(15, 0)

    if weekday == 6:   # Sunday — always closed
        return False
    if weekday == 5:   # Saturday — only 00:00–05:00 (Fri night tail)
        return t < _NIGHT_END
    if weekday == 0:   # Monday — no Sunday night session
        return _DAY_START <= t <= _DAY_END or t >= _NIGHT_START

    # Tuesday – Friday
    if t < _NIGHT_END:                     # 00:00–05:00 night tail
        return True
    if _NIGHT_END <= t < _DAY_START:       # 05:00–08:44 closed
        return False
    if _DAY_START <= t <= _DAY_END:        # 08:45–13:45 day session
        return True
    if _DAY_END < t < _NIGHT_START:        # 13:45–15:00 lunch break / gap
        return False
    return True                            # 15:00+ night session


# ---------------------------------------------------------------------------
# Core job pipeline
# ---------------------------------------------------------------------------

def _run_symbol_job(symbol: str, display_name: str) -> None:
    """
    Complete pipeline for a single symbol:
      fetch 5K + 60K  →  render combined chart  →  upload  →  LINE push

    Errors are raised (not swallowed) so the caller can isolate them
    per-symbol without killing the scheduler.
    """
    now = datetime.now(tz=TW_TZ)
    logger.info(
        "─── [%s] triggered at %s ───",
        display_name, now.strftime("%Y-%m-%d %H:%M:%S"),
    )

    try:
        creds = get_credentials()
    except EnvironmentError as exc:
        logger.error("[%s] Credential error — skipping: %s", display_name, exc)
        return

    chart_path: Optional[Path] = None

    try:
        fetcher = ShioajiDataFetcher(symbol)

        logger.info("[%s] Fetching %d 5-min bars...", display_name, _BARS_5K)
        df_5k = fetcher.fetch_bars("5min", bars=_BARS_5K)

        logger.info("[%s] Fetching %d 60-min bars...", display_name, _BARS_60K)
        df_60k = fetcher.fetch_bars("60min", bars=_BARS_60K)

        logger.info("[%s] Rendering combined 5K+60K chart...", display_name)
        chart_path = render_combined_chart(df_5k, df_60k, display_name,
                                           output_dir=CHARTS_DIR)

        logger.info("[%s] Uploading chart to Imgbb...", display_name)
        image_url = upload_to_imgbb(chart_path, creds["IMGBB_API_KEY"])

        # Price summary
        latest = float(df_5k["Close"].iloc[-1])
        prev   = float(df_5k["Close"].iloc[-2]) if len(df_5k) > 1 else latest
        chg    = latest - prev
        pct    = (chg / prev * 100.0) if prev else 0.0
        arrow  = "▲" if chg >= 0 else "▼"

        push_text = (
            f"[TX-Observer]  {now.strftime('%Y-%m-%d %H:%M')} (UTC+8)\n"
            f"{display_name} ({symbol})\n"
            f"Last:    {latest:>10,.0f}\n"
            f"Change:  {arrow} {abs(chg):.0f}  ({pct:+.2f}%)\n"
            f"Charts:  5K + 60K 合圖"
        )

        logger.info("[%s] Sending LINE push...", display_name)
        success = send_push_message(
            channel_access_token=creds["LINE_CHANNEL_ACCESS_TOKEN"],
            target_id=creds["LINE_TARGET_ID"],
            text=push_text,
            image_url=image_url,
        )

        if success:
            logger.info("[%s] Job completed successfully.", display_name)
        else:
            logger.warning("[%s] Job done — LINE push reported a failure.", display_name)

    except Exception as exc:
        logger.error(
            "[%s] Unhandled error: %s", display_name, exc, exc_info=True
        )
        raise   # re-raise so the caller can count/log per-symbol failures

    finally:
        if chart_path and chart_path.exists():
            try:
                chart_path.unlink()
                logger.info("[%s] Deleted local chart: %s", display_name, chart_path.name)
            except OSError as e:
                logger.warning("[%s] Could not delete chart: %s", display_name, e)


# ---------------------------------------------------------------------------
# Scheduled job entrypoints (one per market type)
# ---------------------------------------------------------------------------

def run_futures_job() -> None:
    """
    Scheduled job for TXF futures (TXFR1).
    Fires at: 08:45 09:45 10:45 11:45 12:45 13:45 (day)
              15:00–23:00 every hour (night early)
              00:00–05:00 every hour (night late)
    """
    now = datetime.now(tz=TW_TZ)
    if not is_trading_time(now, market="futures"):
        logger.info(
            "Futures outside trading hours (%s %s) — skipped.",
            now.strftime("%A"), now.strftime("%H:%M"),
        )
        return

    logger.info("=== Futures job start ===")
    for symbol, name in _FUTURES_SYMBOLS:
        try:
            _run_symbol_job(symbol, name)
        except Exception as exc:
            logger.error(
                "[%s] 任務失敗，繼續執行: %s", name, exc
            )


def run_spot_job() -> None:
    """
    Scheduled job for spot indices (TSE/001, OTC/101).
    Fires at: 09:00 10:00 11:00 12:00 13:00 (Mon–Fri).

    Error isolation: if one index fails, the other still runs.
    """
    now = datetime.now(tz=TW_TZ)
    if not is_trading_time(now, market="spot"):
        logger.info(
            "Spot outside trading hours (%s %s) — skipped.",
            now.strftime("%A"), now.strftime("%H:%M"),
        )
        return

    logger.info("=== Spot job start ===")
    for symbol, name in _SPOT_SYMBOLS:
        try:
            _run_symbol_job(symbol, name)
        except Exception as exc:
            # Log and continue — one index failure must not block the other
            logger.error(
                "[%s] 任務失敗，跳過此品種繼續: %s", name, exc
            )


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def build_scheduler() -> BlockingScheduler:
    """
    Build a BlockingScheduler with cron triggers for all symbols.

    Futures triggers (TXF)
    ──────────────────────
    A. Day session     Mon–Fri  08:45 09:45 10:45 11:45 12:45 13:45
    B. Night (early)   Mon–Fri  15:00 16:00 … 23:00
    C. Night (late)    Tue–Sat  00:00 01:00 … 05:00

    Spot triggers (TSE/OTC)
    ───────────────────────
    D. Day session     Mon–Fri  09:00 10:00 11:00 12:00 13:00

    misfire_grace_time=120 s allows catch-up if the host was briefly
    suspended (e.g. cloud VM live-migration).
    """
    scheduler = BlockingScheduler(timezone=TW_TZ)

    _common_futures = dict(func=run_futures_job, misfire_grace_time=120,
                           replace_existing=True)
    _common_spot    = dict(func=run_spot_job,    misfire_grace_time=120,
                           replace_existing=True)

    # A. TXF day session
    scheduler.add_job(
        **_common_futures,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="8,9,10,11,12,13",
            minute=45,
            second=0,
            timezone=TW_TZ,
        ),
        id="txf_day",
        name="TXF Day Session (08:45–13:45 on :45)",
    )

    # B. TXF night session early (15:00–23:00)
    scheduler.add_job(
        **_common_futures,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="15,16,17,18,19,20,21,22,23",
            minute=0,
            second=0,
            timezone=TW_TZ,
        ),
        id="txf_night_early",
        name="TXF Night Session Early (15:00–23:00)",
    )

    # C. TXF night session late (00:00–05:00, next calendar day)
    scheduler.add_job(
        **_common_futures,
        trigger=CronTrigger(
            day_of_week="tue-sat",
            hour="0,1,2,3,4,5",
            minute=0,
            second=0,
            timezone=TW_TZ,
        ),
        id="txf_night_late",
        name="TXF Night Session Late (00:00–05:00)",
    )

    # D. Spot indices (TSE/OTC)
    scheduler.add_job(
        **_common_spot,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="9,10,11,12,13",
            minute=0,
            second=0,
            timezone=TW_TZ,
        ),
        id="spot_day",
        name="Spot Indices Day Session (09:00–13:00)",
    )

    return scheduler


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="TX-Observer",
        description="Taiwan Futures & Spot K-line chart auto-screenshot & LINE push.",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help=(
            "Execute all jobs immediately regardless of trading hours "
            "(smoke-test the full pipeline), then start the scheduler."
        ),
    )
    parser.add_argument(
        "--symbol",
        metavar="SYM",
        default=None,
        help=(
            "Run --run-now for a specific symbol only "
            "(e.g. TXFR1, TSE/001, OTC/101)."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    logger.info("╔══════════════════════════════════════╗")
    logger.info("║          TX-Observer Starting        ║")
    logger.info("╚══════════════════════════════════════╝")

    # Validate credentials before doing anything else
    try:
        get_credentials()
        logger.info("All credentials loaded successfully.")
    except EnvironmentError as exc:
        logger.error(str(exc))
        sys.exit(1)

    # Initialize Shioaji API once — login happens here, not per-job
    logger.info("Initializing Shioaji API (login once)...")
    try:
        init_api()
        logger.info("Shioaji API ready.")
    except Exception as exc:
        logger.error("Shioaji initialization failed: %s", exc)
        sys.exit(1)

    if args.run_now:
        logger.info("--run-now: executing jobs immediately (trading-hours gate bypassed)...")
        _run_all_now(symbol_filter=args.symbol)

    scheduler = build_scheduler()
    logger.info(
        "Scheduler started (UTC+8). "
        "Triggers: TXF day :45 | TXF night hourly | Spot 9–13:00. "
        "Press Ctrl+C to stop."
    )

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("TX-Observer stopped by user (KeyboardInterrupt).")
    except Exception as exc:
        logger.error("Scheduler crashed: %s", exc, exc_info=True)
        sys.exit(1)


def _run_all_now(symbol_filter: Optional[str] = None) -> None:
    """
    Run all symbol jobs immediately, bypassing the trading-hours gate.
    Used by --run-now for integration smoke-testing.
    """
    all_symbols = _FUTURES_SYMBOLS + _SPOT_SYMBOLS
    if symbol_filter:
        targets = [(s, n) for s, n in all_symbols if s == symbol_filter]
        if not targets:
            logger.warning(
                "--symbol '%s' not found. Available: %s",
                symbol_filter,
                [s for s, _ in all_symbols],
            )
            return
    else:
        targets = all_symbols

    for symbol, name in targets:
        try:
            _run_symbol_job(symbol, name)
        except Exception as exc:
            logger.error("[%s] --run-now failed: %s", name, exc)


if __name__ == "__main__":
    main()
