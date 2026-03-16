"""
main.py — Entry point and scheduler for TX-Observer.

Responsibilities:
  - Bootstrap logging and credential validation
  - Register two APScheduler cron triggers (UTC+8) that cap monthly
    push calls at ~152 (well under the 200-unit LINE free tier)
  - Orchestrate: fetch → resample → render → imgbb upload → LINE push → cleanup

Scheduler design — trigger times (Asia/Taipei):
  ┌─────────────────────────────────────────────────────────────────────┐
  │  Trigger A  │  Mon–Fri   │  09:00  11:00  13:00  15:00  21:00  23:00 │
  │  Trigger B  │  Tue–Sat   │  05:00                                    │
  └─────────────────────────────────────────────────────────────────────┘
  Weekly total : 6 (Mon) + 7×4 (Tue–Fri) + 1 (Sat) = 35 pushes/week
  Monthly avg  : 35 × 4.33 ≈ 152 pushes/month

Trading sessions (UTC+8) — used as a safety gate inside run_job():
  ┌─────────────┬──────────────────────────────────────────────────┐
  │  Day        │  08:45 – 13:45  (regular hours)                  │
  │  Night      │  15:00 – next-day 05:00  (after-hours)           │
  │  Saturday   │  00:00 – 05:00  (Friday night session tail)      │
  │  Sunday     │  Closed                                          │
  │  Mon 00:00  │  Closed until 08:45  (no Sunday night session)   │
  └─────────────┴──────────────────────────────────────────────────┘
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
from apscheduler.triggers.interval import IntervalTrigger

# Bootstrap logging BEFORE importing project modules so all child loggers
# inherit the same console + file handlers.
from config import get_credentials, setup_logging

logger = setup_logging()

# Project modules (imported after logging is configured)
from fetcher import YFinanceDataFetcher                      # noqa: E402
from notifier import send_push_message, upload_to_imgbb     # noqa: E402
from renderer import render_chart                           # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TW_TZ      = pytz.timezone("Asia/Taipei")
CHARTS_DIR = Path("charts")

# 1200 one-minute bars = 20 hours of data
# → gives ~20 usable 60K bars and ~240 usable 5K bars (sufficient for MA240 on 5K)
_FETCH_PERIODS_1MIN = 1200


# ---------------------------------------------------------------------------
# Trading-hours safety gate
# ---------------------------------------------------------------------------

def is_trading_time(dt: datetime) -> bool:
    """
    Return True if *dt* (UTC+8, timezone-aware) falls within a TX Futures
    trading session.

    This gate is a second line of defence.  The cron triggers already target
    only valid session times; this check catches edge cases such as VM clock
    skew or manual --run-now invocations outside market hours.
    """
    weekday = dt.weekday()   # 0 = Monday … 6 = Sunday
    t: dtime = dt.time()

    _NIGHT_END   = dtime(5, 0)
    _DAY_START   = dtime(8, 45)
    _DAY_END     = dtime(13, 45)
    _NIGHT_START = dtime(15, 0)

    # Sunday — always closed
    if weekday == 6:
        return False

    # Saturday — only 00:00–05:00 (Friday night session tail)
    if weekday == 5:
        return t < _NIGHT_END

    # Monday — no Sunday night session; valid only from 08:45 onwards
    if weekday == 0:
        return _DAY_START <= t <= _DAY_END or t >= _NIGHT_START

    # Tuesday – Friday
    if t < _NIGHT_END:                     # 00:00–05:00 night session tail
        return True
    if _NIGHT_END <= t < _DAY_START:       # 05:00–08:44 closed
        return False
    if _DAY_START <= t <= _DAY_END:        # 08:45–13:45 day session
        return True
    if _DAY_END < t < _NIGHT_START:        # 13:45–15:00 lunch break
        return False
    return True                            # 15:00–23:59 night session


# ---------------------------------------------------------------------------
# Scheduled job
# ---------------------------------------------------------------------------

def run_job() -> None:
    """
    Main scheduled task.

    Steps
    -----
    1. Trading-hours gate  — skip + log if outside session
    2. Credential fetch    — abort on misconfiguration
    3. Data fetch          — 480 one-minute bars via MockDataFetcher
    4. Resample            — 5K and 60K DataFrames
    5. Render              — save 60K and 5K charts as local PNGs
    6. Imgbb upload        — upload both PNGs, obtain HTTPS URLs
    7. LINE push           — one Push call with text + 2 image objects
    8. Cleanup             — delete local PNG files (always, in finally)
    """
    now = datetime.now(tz=TW_TZ)
    logger.info("─── Job triggered at %s (UTC+8) ───", now.strftime("%Y-%m-%d %H:%M:%S"))

    # ── Trading-hours gate ─────────────────────────────────────────────────
    # NOTE: gate disabled for testing — re-enable before production
    # if not is_trading_time(now):
    #     logger.info(
    #         "Outside trading hours (%s %s) — job skipped.",
    #         now.strftime("%A"),
    #         now.strftime("%H:%M"),
    #     )
    #     return

    # ── Credentials ────────────────────────────────────────────────────────
    try:
        creds = get_credentials()
    except EnvironmentError as exc:
        logger.error("Credential error — job aborted:\n%s", exc)
        return

    line_token = creds["LINE_CHANNEL_ACCESS_TOKEN"]
    target_id  = creds["LINE_TARGET_ID"]
    imgbb_key  = creds["IMGBB_API_KEY"]

    # Paths tracked here so the finally block can always clean up
    chart_60k_path: Optional[Path] = None
    chart_5k_path:  Optional[Path] = None

    try:
        # ── Fetch ──────────────────────────────────────────────────────────
        logger.info("Fetching %d 1-minute bars via yfinance (%s)...", _FETCH_PERIODS_1MIN, "WTX=F")
        fetcher = YFinanceDataFetcher()
        df_1min = fetcher.fetch_1min_bars(periods=_FETCH_PERIODS_1MIN)

        # ── Resample ───────────────────────────────────────────────────────
        logger.info("Resampling → 5min and 60min...")
        df_5k  = YFinanceDataFetcher.resample_to_timeframe(df_1min, "5min")
        df_60k = YFinanceDataFetcher.resample_to_timeframe(df_1min, "60min")

        # ── Price summary ──────────────────────────────────────────────────
        latest_close = float(df_1min["Close"].iloc[-1])
        prev_close   = float(df_1min["Close"].iloc[-2])
        change       = latest_close - prev_close
        change_pct   = (change / prev_close) * 100.0 if prev_close else 0.0
        arrow        = "▲" if change >= 0 else "▼"

        # ── Render ─────────────────────────────────────────────────────────
        logger.info("Rendering 60K chart...")
        chart_60k_path = render_chart(df_60k, "60K", output_dir=CHARTS_DIR)

        logger.info("Rendering 5K chart...")
        chart_5k_path = render_chart(df_5k, "5K", output_dir=CHARTS_DIR)

        # ── Imgbb upload ───────────────────────────────────────────────────
        # Upload both charts before sending LINE push.
        # If an upload fails, RuntimeError is raised and caught below;
        # the finally block still cleans up local files.
        logger.info("Uploading charts to Imgbb...")
        url_60k = upload_to_imgbb(chart_60k_path, imgbb_key)
        url_5k  = upload_to_imgbb(chart_5k_path,  imgbb_key)

        # ── Compose push text ──────────────────────────────────────────────
        push_text = (
            f"[TX-Observer]  {now.strftime('%Y-%m-%d %H:%M')} (UTC+8)\n"
            f"TX Futures — Near Month\n"
            f"Last:    {latest_close:>10,.0f}\n"
            f"Change:  {arrow} {abs(change):,.0f}  ({change_pct:+.2f}%)\n"
            f"Charts:  60K + 5K"
        )

        # ── LINE push ──────────────────────────────────────────────────────
        logger.info("Sending LINE push message...")
        success = send_push_message(
            channel_access_token=line_token,
            target_id=target_id,
            text=push_text,
            image_url_60k=url_60k,
            image_url_5k=url_5k,
        )

        if success:
            logger.info("Job completed successfully.")
        else:
            logger.warning("Job completed — LINE push reported a failure (see above).")

    except Exception as exc:
        logger.error("Job encountered an unhandled error: %s", exc, exc_info=True)

    finally:
        # ── Local file cleanup ─────────────────────────────────────────────
        # Always runs, even if render or upload raised an exception.
        for path in (chart_60k_path, chart_5k_path):
            if path and path.exists():
                try:
                    path.unlink()
                    logger.info("Deleted local chart: %s", path.name)
                except OSError as exc:
                    logger.warning("Could not delete %s: %s", path.name, exc)


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def build_scheduler() -> BlockingScheduler:
    """
    Build an APScheduler BlockingScheduler with two cron triggers (UTC+8).

    Trigger A — weekday daytime + night sessions (Mon–Fri):
        Fires at 09:00, 11:00, 13:00, 15:00, 21:00, 23:00

    Trigger B — night-session close (Tue–Sat):
        Fires at 05:00  (captures Mon–Fri night session endings)

    misfire_grace_time=120 s allows the job to still run if the host was
    briefly suspended (e.g. cloud VM live-migration) and the trigger fired
    up to 120 s ago.
    """
    scheduler = BlockingScheduler(timezone=TW_TZ)

    _common = dict(
        func=run_job,
        misfire_grace_time=120,
        replace_existing=True,
    )

    # NOTE: testing mode — fires every 1 minute
    scheduler.add_job(
        **_common,
        trigger=IntervalTrigger(minutes=1, timezone=TW_TZ),
        id="tx_test_interval",
        name="TX Observer — test (every 1 min)",
    )

    # ---- production triggers (re-enable when done testing) ----
    # # Trigger A: weekday session snapshots
    # scheduler.add_job(
    #     **_common,
    #     trigger=CronTrigger(
    #         day_of_week="mon-fri",
    #         hour="9,11,13,15,21,23",
    #         minute=0,
    #         second=0,
    #         timezone=TW_TZ,
    #     ),
    #     id="tx_weekday_sessions",
    #     name="TX Observer — weekday session snapshots",
    # )
    # # Trigger B: night-session close (early morning)
    # scheduler.add_job(
    #     **_common,
    #     trigger=CronTrigger(
    #         day_of_week="tue-sat",
    #         hour=5,
    #         minute=0,
    #         second=0,
    #         timezone=TW_TZ,
    #     ),
    #     id="tx_night_close",
    #     name="TX Observer — night session close (05:00)",
    # )

    return scheduler


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="TX-Observer",
        description="Taiwan Futures (TX) K-line chart auto-screenshot & LINE push system.",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help=(
            "Execute one job immediately regardless of trading hours "
            "(for smoke-testing the full pipeline), then start the scheduler."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    logger.info("╔══════════════════════════════════════╗")
    logger.info("║          TX-Observer Starting        ║")
    logger.info("╚══════════════════════════════════════╝")

    # Validate all credentials at startup — fail fast before the first job
    try:
        get_credentials()
        logger.info("All credentials loaded successfully.")
    except EnvironmentError as exc:
        logger.error(str(exc))
        sys.exit(1)

    if args.run_now:
        logger.info("--run-now: executing one job immediately (ignoring trading-hours gate)...")
        # Temporarily bypass the gate by calling _run_job_unconditional
        _run_job_unconditional()

    scheduler = build_scheduler()
    logger.info(
        "Scheduler started. [TEST MODE] Trigger: every 1 minute. "
        "Press Ctrl+C to stop."
    )

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("TX-Observer stopped by user (KeyboardInterrupt).")
    except Exception as exc:
        logger.error("Scheduler crashed: %s", exc, exc_info=True)
        sys.exit(1)


def _run_job_unconditional() -> None:
    """
    Run one complete job cycle without the trading-hours gate.
    Used only by --run-now for integration testing.
    """
    # Monkey-patch is_trading_time for this one call
    import main as _self
    _orig = _self.is_trading_time
    _self.is_trading_time = lambda _dt: True
    try:
        run_job()
    finally:
        _self.is_trading_time = _orig


if __name__ == "__main__":
    main()
