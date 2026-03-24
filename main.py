"""
main.py — Entry point and scheduler for TX-Observer.

Supported symbols
-----------------
  TXFR1   : 台指期貨近一連續合約  (futures, day + night session)
  TSE/001 : 加權指數              (spot, Mon–Fri 09:00–13:30)
  OTC/101 : 櫃買指數              (spot, Mon–Fri 09:00–13:30)

Scheduler design — trigger times (Asia/Taipei)  ← all triggers fire at :10 s
-----------------------------------------------
  ┌────────────────┬──────────────────────────────────────────────────────┐
  │  Futures (TXF) │  Day   : 08:45  09:45  10:45  11:45  12:45          │
  │                │  Close : 13:45  (dedicated closing, retry×3)        │
  │                │  Night : 15:00  16:00  17:00  18:00  19:00  20:00   │
  │                │          21:00  22:00  23:00  (Mon–Fri)             │
  │                │          00:00  01:00  02:00  03:00  04:00  05:00   │
  │                │          (Tue–Sat, 05:00 = 夜盤收盤)                 │
  ├────────────────┼──────────────────────────────────────────────────────┤
  │  Spot (TSE/OTC)│  Day   : 09:00  10:00  11:00  12:00  13:00          │
  │                │  Close : 13:45  (dedicated closing, retry×3)        │
  │                │  (no night session)                                  │
  └────────────────┴──────────────────────────────────────────────────────┘

  The 10-second delay ensures the exchange has finished writing the just-
  closed bar to the database before we query it.

  Closing summary jobs (13:45:10 for both futures and spot) are separate
  dedicated triggers.  They fire with a "【今日收盤總結】" label in the
  LINE push and retry up to 3 times (5-second gap) to handle late API
  data delivery at session close.

Chart modes
-----------
  Futures (TXFR1) : 60K (65 bars, upper) + 5K  (90 bars, lower)
  Spot (TSE/OTC)  : 日K (45 bars, upper) + 60K (65 bars, lower)

Error isolation
---------------
  If one symbol fails (fetch / render / upload / push), the error is logged
  and execution continues to the next symbol without crashing the scheduler.
  Closing jobs retry up to _CLOSING_MAX_RETRIES times before giving up.

Shioaji login
-------------
  The API singleton is initialized once at startup via fetcher.init_api().
  Individual job calls reuse the cached singleton — no re-login per task.
  Token-expiry auto re-login is handled inside fetcher.py (up to 2 retries
  per fetch call), covering cross-weekend session invalidation.
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Optional

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# ---------------------------------------------------------------------------
# 全域時區硬化 — 必須在所有其他初始化之前執行（含 setup_logging 之前）
# ---------------------------------------------------------------------------
# UTC 伺服器上若不先設定 TZ，logging 的 %(asctime)s 與 datetime.now() 在
# 未傳入 tz= 時都會輸出 UTC，導致凌晨日期計算偏移 8 小時。
# os.environ["TZ"] 在 Linux 上搭配 time.tzset() 可讓整個 process 的 C 層
# localtime() 也指向台北時間，是最徹底的修復方式。
os.environ["TZ"] = "Asia/Taipei"
if hasattr(time, "tzset"):
    time.tzset()

try:
    import pandas_market_calendars as mcal
    _HAS_MCAL = True
except ImportError:
    _HAS_MCAL = False

from config import get_credentials, setup_logging

logger = setup_logging()

from fetcher import ShioajiDataFetcher, init_api, _force_relogin, _get_api  # noqa: E402
from notifier import send_push_message, upload_to_imgbb  # noqa: E402
from renderer import render_combined_chart           # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TW_TZ      = pytz.timezone("Asia/Taipei")
CHARTS_DIR = Path("charts")

# Display bars requested from fetcher.  fetcher.py internally fetches a larger
# buffer (≥ _MA_COMPUTE_MIN_BARS = 500) for accurate MA computation, then slices
# to these counts before returning.  The returned DataFrames already include
# pre-computed MA5/10/20/60/240 columns so renderer.py skips recomputation.
_DISPLAY_5K    = 90    # 台指期 5K  顯示根數
_DISPLAY_60K   = 65    # 共用   60K 顯示根數
_DISPLAY_DAILY = 45    # 現貨  日K  顯示根數

# Closing summary retry policy — applied to both 13:45 spot and 13:45 futures
# jobs to handle API data delivery delays at session close.
_CLOSING_MAX_RETRIES = 3
_CLOSING_RETRY_DELAY = 5.0   # seconds between retries

# Initialization retry policy — covers IndexError / connection errors during
# api.login() + api.fetch_contracts() at startup (e.g. during exchange settlement).
_INIT_MAX_RETRIES = 5
_INIT_RETRY_DELAY = 10.0   # seconds between initialization attempts

# Symbols dispatched in each job
_FUTURES_SYMBOLS: list[tuple[str, str]] = [
    ("TXFR1", "台指期近一"),
]
_SPOT_SYMBOLS: list[tuple[str, str]] = [
    ("TSE/001", "加權指數"),
    ("OTC/101", "櫃買指數"),
]

# Spot symbols that use the 日K+60K chart mode
_SPOT_SYMBOL_SET: frozenset[str] = frozenset(s for s, _ in _SPOT_SYMBOLS)

# ---------------------------------------------------------------------------
# Initialization with retry (合約抓取重試)
# ---------------------------------------------------------------------------

def _verify_contracts() -> None:
    """
    確認 fetch_contracts() 已成功載入 TXFR1 合約物件。

    Shioaji 在交易所結算期間偶爾會回傳空白合約表；此函式在初始化成功後
    立即驗證，若合約物件為 None 則拋出 RuntimeError，觸發上層重試邏輯。
    """
    api = _get_api()
    txf_group = getattr(getattr(api.Contracts, "Futures", None), "TXF", None)
    contract  = getattr(txf_group, "TXFR1", None) if txf_group is not None else None

    if contract is None:
        raise RuntimeError(
            "TXFR1 合約物件為 None — fetch_contracts() 可能尚未完成或結算中。"
        )
    logger.info("合約驗證通過：TXFR1 合約物件已確認。")


def _init_api_with_retry() -> None:
    """
    執行 Shioaji login + fetch_contracts，失敗時每隔 _INIT_RETRY_DELAY 秒自動重試，
    最多重試 _INIT_MAX_RETRIES 次。

    涵蓋場景
    --------
    - IndexError: list assignment index out of range（交易所結算期間 contracts 異常）
    - 連線逾時 / Token 初始化失敗
    - fetch_contracts 靜默成功但合約物件為空

    每次重試前會呼叫 _force_relogin() 確保舊的 singleton 被清除，
    避免帶著損壞的 session 重試。
    """
    last_exc: Exception = RuntimeError("未執行任何初始化嘗試")

    for attempt in range(1, _INIT_MAX_RETRIES + 1):
        try:
            init_api()
            _verify_contracts()
            logger.info(
                "Shioaji API 初始化成功（第 %d/%d 次嘗試）。",
                attempt, _INIT_MAX_RETRIES,
            )
            return   # 成功 — 退出重試迴圈

        except IndexError as exc:
            last_exc = exc
            logger.warning(
                "[WARNING] 伺服器結算中或合約抓取異常，稍後將自動重試..."
                " (第 %d/%d 次，錯誤: %s)",
                attempt, _INIT_MAX_RETRIES, exc,
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "[WARNING] 伺服器結算中或合約抓取異常，稍後將自動重試..."
                " (第 %d/%d 次，錯誤: %s)",
                attempt, _INIT_MAX_RETRIES, exc,
            )

        if attempt < _INIT_MAX_RETRIES:
            logger.info(
                "等待 %.0f 秒後執行第 %d 次重新初始化...",
                _INIT_RETRY_DELAY, attempt + 1,
            )
            # 清除損壞的 singleton，確保下一次 init_api() 重新建立連線
            try:
                _force_relogin()
            except Exception:
                pass   # 重置失敗無妨，_get_api() 會重新建立
            time.sleep(_INIT_RETRY_DELAY)

    raise RuntimeError(
        f"Shioaji 初始化失敗，已重試 {_INIT_MAX_RETRIES} 次，最終錯誤: {last_exc}"
    )


# ---------------------------------------------------------------------------
# Market state — updated at startup and daily at 08:30 by run_market_check()
# ---------------------------------------------------------------------------
_market_open_today: bool = True   # default True; corrected before scheduler starts


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
# Market calendar — is today a Taiwan trading day?
# ---------------------------------------------------------------------------

def is_market_open_today(dt: datetime) -> bool:
    """
    Return True if *dt* (UTC+8) falls on a Taiwan Stock Exchange trading day.

    Uses pandas_market_calendars XTAI calendar when available; this correctly
    handles public holidays, Chinese New Year closures, and Saturday make-up
    sessions.  Falls back to a simple Mon–Fri weekday check when the library
    is not installed (with a one-time warning logged).
    """
    if not _HAS_MCAL:
        logger.warning(
            "pandas_market_calendars not installed — holiday detection disabled. "
            "Install with:  pip install pandas-market-calendars"
        )
        return dt.weekday() < 5   # Mon–Fri only

    try:
        cal       = mcal.get_calendar("XTAI")
        date_str  = dt.strftime("%Y-%m-%d")
        schedule  = cal.schedule(start_date=date_str, end_date=date_str)
        return not schedule.empty
    except Exception as exc:
        logger.warning("Market calendar check failed (%s) — assuming open.", exc)
        return True


# ---------------------------------------------------------------------------
# Settlement day — third Wednesday of each month (台指期月結算)
# ---------------------------------------------------------------------------

def is_settlement_day(dt: datetime) -> bool:
    """
    Return True if *dt* is the third Wednesday of its month.

    The third Wednesday always falls between the 15th and 21st (inclusive).
    This is a closed-form check that handles all months without iteration.

    Examples
    --------
    2026-03-18 (Wed, day 18)  →  True   (3rd Wed of March 2026)
    2026-03-11 (Wed, day 11)  →  False  (2nd Wed)
    2026-03-18 (Thu)          →  False  (not a Wednesday)
    """
    return dt.weekday() == 2 and 15 <= dt.day <= 21


# ---------------------------------------------------------------------------
# Daily market check — runs at 08:30 and once at startup
# ---------------------------------------------------------------------------

def run_market_check() -> None:
    """
    Refresh *_market_open_today* for the current date.

    Scheduled daily at 08:30 (Asia/Taipei) so the flag is set before the
    first trading job fires at 08:45.  Also called once during startup so
    the flag is correct even if the process starts after 08:30.
    """
    global _market_open_today
    now = datetime.now(tz=TW_TZ)
    _market_open_today = is_market_open_today(now)
    if _market_open_today:
        logger.info(
            "今日台股開盤（%s），排程任務正常執行。",
            now.strftime("%Y-%m-%d %A"),
        )
    else:
        logger.info(
            "今日為台股休市日（%s），系統進入休眠模式。",
            now.strftime("%Y-%m-%d %A"),
        )


# ---------------------------------------------------------------------------
# Core job pipeline
# ---------------------------------------------------------------------------

def _run_symbol_job(
    symbol: str,
    display_name: str,
    closing_summary: bool = False,
) -> None:
    """
    Complete pipeline for a single symbol:

    Futures (TXFR1)
      fetch 60K + 5K  →  render 60K+5K chart  →  upload  →  LINE push

    Spot (TSE/001 / OTC/101)
      fetch 1min → resample to 日K
      fetch 60K
      →  render 日K+60K chart  →  upload  →  LINE push

    Parameters
    ----------
    closing_summary : When True the LINE push is prefixed with
                      "【今日收盤總結】" to identify end-of-session charts.

    Errors are raised (not swallowed) so the caller can isolate them
    per-symbol without killing the scheduler.

    Token-expiry auto re-login is handled transparently inside fetcher.py
    (up to 2 retries per fetch call via _force_relogin()), so no additional
    re-login logic is required here.
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

    is_spot    = symbol in _SPOT_SYMBOL_SET
    chart_path: Optional[Path] = None

    try:
        fetcher = ShioajiDataFetcher(symbol)

        if is_spot:
            # ── 現貨模式：日K (上, 45根) + 60K (下, 65根) ───────────────────
            # fetcher 內部抓取 ≥300 根日K 計算 MA240，切片後傳回 _DISPLAY_DAILY 根。
            logger.info("[%s] Fetching %d daily bars (with MA buffer)...",
                        display_name, _DISPLAY_DAILY)
            df_upper = fetcher.fetch_bars("1day",  bars=_DISPLAY_DAILY)

            logger.info("[%s] Fetching %d 60-min bars...", display_name, _DISPLAY_60K)
            df_60k = fetcher.fetch_bars("60min", bars=_DISPLAY_60K)

            logger.info("[%s] Rendering combined 日K+60K chart...", display_name)
            chart_path = render_combined_chart(
                df_upper, df_60k, display_name,
                output_dir=CHARTS_DIR,
                mode="spot",
            )
            chart_desc = "日K + 60K 合圖"

        else:
            # ── 期貨模式：60K (上, 65根) + 5K (下, 90根) ────────────────────
            # fetcher 內部抓取 ≥300 根計算 MA240，切片後傳回目標根數。
            logger.info("[%s] Fetching %d 5-min bars...", display_name, _DISPLAY_5K)
            df_upper = fetcher.fetch_bars("5min",  bars=_DISPLAY_5K)

            logger.info("[%s] Fetching %d 60-min bars...", display_name, _DISPLAY_60K)
            df_60k = fetcher.fetch_bars("60min", bars=_DISPLAY_60K)

            logger.info("[%s] Rendering combined 5K+60K chart...", display_name)
            chart_path = render_combined_chart(
                df_upper, df_60k, display_name,
                output_dir=CHARTS_DIR,
                mode="futures",
            )
            chart_desc = "5K + 60K 合圖"

        logger.info("[%s] Uploading chart to Imgbb...", display_name)
        image_url = upload_to_imgbb(chart_path, creds["IMGBB_API_KEY"])

        # Price summary — use upper-panel (most granular recent close)
        latest = float(df_upper["Close"].iloc[-1])
        prev   = float(df_upper["Close"].iloc[-2]) if len(df_upper) > 1 else latest
        chg    = latest - prev
        pct    = (chg / prev * 100.0) if prev else 0.0
        arrow  = "▲" if chg >= 0 else "▼"

        # Closing summary prefix — applied when this is an end-of-session job
        closing_prefix = "【今日收盤總結】\n" if closing_summary else ""

        # Settlement day prefix — TXFR1 only, third Wednesday of each month
        settlement_prefix = ""
        if symbol == "TXFR1" and is_settlement_day(now):
            settlement_prefix = "【今日台指結算日】\n"
            logger.info("[%s] 今日為台指期結算日。", display_name)

        push_text = (
            closing_prefix
            + settlement_prefix
            + f"[TX-Observer]  {now.strftime('%Y-%m-%d %H:%M')} (UTC+8)\n"
            f"{display_name} ({symbol})\n"
            f"Last:    {latest:>10,.0f}\n"
            f"Change:  {arrow} {abs(chg):.0f}  ({pct:+.2f}%)\n"
            f"Charts:  {chart_desc}"
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
    Fires at: 08:45:10  09:45:10 … 13:45:10  (day)
              15:00:10–23:00:10 every hour    (night early)
              00:00:10–05:00:10 every hour    (night late)
    """
    if not _market_open_today:
        return   # already logged in run_market_check()

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
    Fires at: 09:00:10  10:00:10  11:00:10  12:00:10  13:00:10  (Mon–Fri).

    Error isolation: if one index fails, the other still runs.
    """
    if not _market_open_today:
        return   # already logged in run_market_check()

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
# Retry wrapper — used by closing summary jobs
# ---------------------------------------------------------------------------

def _run_symbol_job_with_retry(
    symbol: str,
    display_name: str,
    closing_summary: bool = False,
    max_retries: int = _CLOSING_MAX_RETRIES,
    retry_delay: float = _CLOSING_RETRY_DELAY,
) -> None:
    """
    Execute _run_symbol_job() with automatic retry on failure.

    Retries up to *max_retries* times with *retry_delay* seconds between
    attempts.  Designed for closing summary jobs where API data delivery
    may lag slightly after the session-close timestamp.

    Errors are swallowed after the final attempt so one symbol failure
    never propagates to the next symbol in the same job.
    """
    for attempt in range(1, max_retries + 1):
        try:
            _run_symbol_job(symbol, display_name, closing_summary=closing_summary)
            return   # success — exit retry loop
        except Exception as exc:
            if attempt < max_retries:
                logger.warning(
                    "[%s] 第 %d/%d 次失敗，%g 秒後重試: %s",
                    display_name, attempt, max_retries, retry_delay, exc,
                )
                time.sleep(retry_delay)
            else:
                logger.error(
                    "[%s] 已重試 %d 次，最終失敗: %s",
                    display_name, max_retries, exc,
                )


# ---------------------------------------------------------------------------
# Closing summary job entrypoints
# ---------------------------------------------------------------------------

def run_spot_closing_job() -> None:
    """
    現貨收盤總結任務 — TSE/001 + OTC/101.

    Fires at 13:30:10 (Mon–Fri) — 10 s after the spot market close.
    Renders 日K (上, 45根) + 60K (下, 65根) to capture the final daily bar.
    Retries up to _CLOSING_MAX_RETRIES times with 5-second intervals to
    handle late data delivery at session end.
    """
    if not _market_open_today:
        return

    now = datetime.now(tz=TW_TZ)
    logger.info("=== Spot Closing Summary job start (%s) ===", now.strftime("%H:%M:%S"))

    for symbol, name in _SPOT_SYMBOLS:
        _run_symbol_job_with_retry(symbol, name, closing_summary=True)


def run_futures_closing_job() -> None:
    """
    期貨收盤總結任務 — TXFR1.

    Fires at 13:45:10 (Mon–Fri) — 10 s after the futures day-session close.
    Renders 60K (上, 65根) + 5K (下, 90根) to confirm the final 5K and 60K
    bars are fully settled.
    Retries up to _CLOSING_MAX_RETRIES times with 5-second intervals.
    """
    if not _market_open_today:
        return

    now = datetime.now(tz=TW_TZ)
    logger.info("=== Futures Closing Summary job start (%s) ===", now.strftime("%H:%M:%S"))

    for symbol, name in _FUTURES_SYMBOLS:
        _run_symbol_job_with_retry(symbol, name, closing_summary=True)


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def build_scheduler() -> BlockingScheduler:
    """
    Build a BlockingScheduler with cron triggers for all symbols.

    All triggers fire at second=10 (10 s after the bar close) to ensure the
    exchange database has fully committed the just-closed bar.

    Futures triggers (TXF)
    ──────────────────────
    A. Day session (regular)  Mon–Fri  08:45  09:45  10:45  11:45  12:45
    B. Futures closing        Mon–Fri  13:45:10  (dedicated, retry×3)
    C. Night (early)          Mon–Fri  15:00  16:00  17:00  18:00  19:00
                                        20:00  21:00  22:00  23:00
    D. Night (late)           Tue–Sat  00:00  01:00  02:00  03:00  04:00  05:00

    Spot triggers (TSE/OTC)
    ───────────────────────
    E. Day session (regular)  Mon–Fri  09:00  10:00  11:00  12:00  13:00
    F. Spot closing           Mon–Fri  13:45:10  (dedicated, retry×3)

    Other
    ─────
    G. Daily market-open check  Mon–Sat  08:30:00

    misfire_grace_time=120 s allows catch-up if the host was briefly
    suspended (e.g. cloud VM live-migration).
    Closing jobs use misfire_grace_time=300 s for extra tolerance.
    """
    scheduler = BlockingScheduler(timezone=TW_TZ)

    _common_futures  = dict(func=run_futures_job,         misfire_grace_time=120,
                            replace_existing=True)
    _common_spot     = dict(func=run_spot_job,            misfire_grace_time=120,
                            replace_existing=True)
    _common_closing  = dict(misfire_grace_time=300, replace_existing=True)

    # A. TXF day session (regular) — fires 10 s after each :45 bar close
    #    Hour 13 is intentionally excluded: the 13:45 close is handled by
    #    the dedicated futures closing job (B) to enable retry logic and the
    #    【今日收盤總結】 LINE push label.
    scheduler.add_job(
        **_common_futures,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="8,9,10,11,12",
            minute=45,
            second=10,
            timezone=TW_TZ,
        ),
        id="txf_day",
        name="TXF Day Session regular (08:45:10–12:45:10 on :45)",
    )

    # B. TXF closing summary — 13:45:10, with retry + 【今日收盤總結】 push
    scheduler.add_job(
        func=run_futures_closing_job,
        **_common_closing,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=13,
            minute=45,
            second=10,
            timezone=TW_TZ,
        ),
        id="txf_closing",
        name="TXF Closing Summary (13:45:10)",
    )

    # C. TXF night session early — every hour 15:00–23:00 (Mon–Fri).
    #    Covers the full 15:00 open through 23:00 checkpoint.
    scheduler.add_job(
        **_common_futures,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="15,16,17,18,19,20,21,22,23",
            minute=0,
            second=10,
            timezone=TW_TZ,
        ),
        id="txf_night_early",
        name="TXF Night Session Early (15:00:10–23:00:10)",
    )

    # D. TXF night session late — every hour 00:00–05:00 (Tue–Sat).
    #    Covers the cross-midnight tail through 05:00 夜盤收盤.
    scheduler.add_job(
        **_common_futures,
        trigger=CronTrigger(
            day_of_week="tue-sat",
            hour="0,1,2,3,4,5",
            minute=0,
            second=10,
            timezone=TW_TZ,
        ),
        id="txf_night_late",
        name="TXF Night Session Late (00:00:10–05:00:10)",
    )

    # E. Spot indices (TSE/OTC) — fires 10 s after each :00 bar close
    scheduler.add_job(
        **_common_spot,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="9,10,11,12,13",
            minute=0,
            second=10,
            timezone=TW_TZ,
        ),
        id="spot_day",
        name="Spot Indices Day Session (09:00:10–13:00:10)",
    )

    # F. Spot closing summary — 13:45:10, with retry + 【今日收盤總結】 push.
    #    Fires 15 minutes after the 13:30 spot close; 13:45 is shared with the
    #    futures closing trigger (B) and both run concurrently without conflict.
    scheduler.add_job(
        func=run_spot_closing_job,
        **_common_closing,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=13,
            minute=45,
            second=10,
            timezone=TW_TZ,
        ),
        id="spot_closing",
        name="Spot Closing Summary (13:45:10)",
    )

    # G. Daily market-open check (08:30) — must fire before 08:45 futures job.
    #    mon-sat covers Saturday make-up sessions where the market reopens.
    #    misfire_grace_time=300 s so the check still runs after a brief VM resume.
    scheduler.add_job(
        func=run_market_check,
        trigger=CronTrigger(
            day_of_week="mon-sat",
            hour=8,
            minute=30,
            second=0,
            timezone=TW_TZ,
        ),
        id="market_check",
        name="Daily Market Open Check (08:30)",
        misfire_grace_time=300,
        replace_existing=True,
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

    # Initialize Shioaji API once — login + contract fetch with auto-retry
    logger.info(
        "Initializing Shioaji API (最多重試 %d 次，間隔 %.0f 秒)...",
        _INIT_MAX_RETRIES, _INIT_RETRY_DELAY,
    )
    try:
        _init_api_with_retry()
    except RuntimeError as exc:
        logger.error("Shioaji 初始化最終失敗，程式中止: %s", exc)
        sys.exit(1)

    # Initialise the market-open flag immediately so the correct state is known
    # before the scheduler's first 08:30 check fires.
    run_market_check()

    if args.run_now:
        logger.info("--run-now: executing jobs immediately (trading-hours gate bypassed)...")
        _run_all_now(symbol_filter=args.symbol)

    scheduler = build_scheduler()
    logger.info(
        "Scheduler started (UTC+8). "
        "TXF day: 08:45–12:45 + closing 13:45 | "
        "TXF night: 15:00–23:00 + 00:00–05:00 (every hour) | "
        "Spot: 09:00–13:00 + closing 13:45. "
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
