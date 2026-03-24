"""
fetcher.py — Data fetching, resampling, and MA pre-computation for TX-Observer.

Supports four symbols:
  - TXFR1   : 台指期貨近一連續合約 (futures, day + night session)
  - TSE/001 : 加權指數 (spot, 09:00–13:30)
  - OTC/101 : 櫃買指數 (spot, 09:00–13:30)

All symbols share the same Shioaji API singleton — login happens once at
process startup via init_api() / _get_api().

Timeframe support
-----------------
  "1min", "5min", "10min", "15min", "30min", "60min"  — futures + spot
  "1day"                                               — spot only
      Internally fetches 1-min bars and resamples to daily.

MA pre-computation
------------------
  fetch_bars() always computes MA5/10/20/60/240 on a FULL buffer dataset
  (≥ _MA_BUFFER_PER_TF[timeframe] resampled bars) BEFORE slicing to the
  requested display window.  The returned DataFrame contains pre-computed
  MA columns so renderer.py can skip recomputation and still display
  correct long-period MA values (e.g. MA240) even for small display windows.

  Per-timeframe buffer strategy:
    "1day"  : fetch_count = max(bars, 300)   → display  45  daily bars
    "60min" : fetch_count = max(bars, 500)   → display  65  hourly bars
    "5min"  : fetch_count = max(bars, 300)   → display  90  5-min bars
    → compute MA on fetch_count bars         (tail values match XQ)
    → slice to bars                          (display window with correct MA)

  If the API cannot return enough data for a given MA period, a warning
  is logged but no exception is raised — the MA column simply contains NaN.

Resample convention
-------------------
TXFR1 5K  : session-isolated resample with offset='45min' (XQ-standard bins)
TXFR1 60K : custom groupby using _get_60k_label() — different cut-points for
             day (cut at :46) and night (cut at :01) sessions
Spot  5K  : session-isolated resample, standard bins (no offset)
Spot  60K : custom groupby using _get_60k_label_spot() — cut at :01 / 13:30
Spot  1D  : per-calendar-date groupby of the spot 09:00–13:30 session

Token expiry
------------
Cross-weekend session invalidation is handled transparently: each call to
api.kbars() retries up to twice after _force_relogin() if a TokenError
(HTTP 401) is detected, covering double-expiry on long-running processes.
"""

import atexit
import logging
import math
import os
import time as _time_mod
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import pytz
import shioaji as sj
from dotenv import load_dotenv

# ===========================================================================
# 全域時區硬化 — 必須在所有其他初始化之前執行
# ===========================================================================
# 在 UTC 伺服器上，os.environ["TZ"] 讓 C 層 localtime() / mktime() 也回傳
# 台北時間，確保 Shioaji SDK 內部若有依賴 localtime 的邏輯都能對齊 CST。
# config.py 的 setup_logging() 也會設定此值，但 fetcher 可能被單獨 import，
# 因此在此處再次確認，保持冪等（idempotent）。
os.environ.setdefault("TZ", "Asia/Taipei")
if hasattr(_time_mod, "tzset"):
    _time_mod.tzset()

# Shioaji TokenError — used for precise 401 detection
try:
    from shioaji.error import TokenError as _SjTokenError
except ImportError:
    _SjTokenError = None  # type: ignore[assignment,misc]

# pandas >= 1.1 replaced resample(base=) with resample(offset=)
_PD_GTE_1_1 = tuple(int(x) for x in pd.__version__.split(".")[:2]) >= (1, 1)

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logger = logging.getLogger("tx_observer.fetcher")

TW_TZ = pytz.timezone("Asia/Taipei")


# ===========================================================================
# Trading session windows (UTC+8)
# ===========================================================================

# TXF futures session
_DAY_START   = dtime(8, 45)
_DAY_END     = dtime(13, 45)
_NIGHT_START = dtime(15, 0)
_NIGHT_END   = dtime(5, 0)

# Spot index session (TSE / OTC)
_SPOT_DAY_START = dtime(9, 0)
_SPOT_DAY_END   = dtime(13, 30)

# Gap threshold for session-block detection
_SESSION_GAP = pd.Timedelta(minutes=70)

# Conservative 1-min bars per trading day estimates
_TRADING_MIN_PER_DAY      = 480   # TXF  (day + night session)
_SPOT_TRADING_MIN_PER_DAY = 270   # TSE/OTC (09:00–13:30)

# Spot symbols → (Shioaji market group, contract code)
_SPOT_SYMBOLS: dict[str, tuple[str, str]] = {
    "TSE/001": ("TSE", "TSE001"),
    "OTC/101": ("OTC", "OTC101"),
}


# ===========================================================================
# MA pre-computation configuration
# ===========================================================================

# MA periods that fetch_bars() will compute and attach to the returned df.
# Must match renderer._MA_PERIODS so that renderer can detect and reuse them.
_MA_PERIODS_COMPUTE: list[int] = [5, 10, 20, 60, 240]

# Per-timeframe MA computation buffer sizes.
#
# Before slicing to the display window, fetch_bars() always fetches at least
# this many *resampled* bars so that MA240 (年線) has a valid tail value:
#
#   "1day"  → 300 daily bars  ≈ 14 months  (display: 45)
#   "60min" → 500 hourly bars ≈ 20 weeks   (display: 65)
#   "5min"  → 300 5-min bars  ≈ 3–4 days   (display: 90)
#
# All other timeframes fall back to the default (300).
_MA_BUFFER_PER_TF: dict[str, int] = {
    "1day":  300,
    "60min": 500,
    "5min":  300,
}
_MA_COMPUTE_MIN_BARS: int = 300   # fallback for unlisted timeframes

# Approximate trading minutes per resampled bar — used to calculate how many
# 1-min bars the API needs to cover for _MA_COMPUTE_MIN_BARS resampled bars.
# "1day" uses the spot session length so the lookback covers enough days.
_TIMEFRAME_MIN: dict[str, int] = {
    "1min":  1,
    "5min":  5,
    "10min": 10,
    "15min": 15,
    "30min": 30,
    "60min": 60,
    "1day":  _SPOT_TRADING_MIN_PER_DAY,   # ~270 min/trading day
}


# ===========================================================================
# Session classification helpers
# ===========================================================================

def _get_trading_session(t: dtime) -> "str | None":
    """TXF session: 'day' (08:45–13:45), 'night' (15:00–05:00), or None."""
    if _DAY_START <= t <= _DAY_END:
        return "day"
    if t >= _NIGHT_START:
        return "night"
    if t <= _NIGHT_END:
        return "night"
    return None


def _get_spot_trading_session(t: dtime) -> "str | None":
    """TSE/OTC spot session: 09:00–13:30 only."""
    if _SPOT_DAY_START <= t <= _SPOT_DAY_END:
        return "day"
    return None


# ===========================================================================
# 60K label functions (XQ-standard cut points)
# ===========================================================================

def _get_60k_label(ts: pd.Timestamp) -> pd.Timestamp:
    """
    Map a TXF 1-min bar to its XQ-standard 60K bar label.

    Day session (cut at :46):
      08:45 → 09:45,  09:46 → 10:45,  …,  12:46 → 13:45

    Night session (cut at :01):
      15:00 → 16:00,  16:01 → 17:00,  …,  04:01 → 05:00
    """
    t  = ts.time()
    fl = ts.floor("min")

    session = _get_trading_session(t)
    if session is None:
        return pd.NaT

    if session == "day":
        if t.minute >= 46 or (t.hour == 8 and t.minute == 45):
            return fl.replace(hour=t.hour + 1, minute=45)
        else:
            return fl.replace(minute=45)
    else:
        if t.minute == 0 and t.hour != 15:
            return fl
        else:
            return (fl + pd.Timedelta(hours=1)).replace(minute=0)


def _get_60k_label_spot(ts: pd.Timestamp) -> pd.Timestamp:
    """
    Map a TSE/OTC spot index 1-min bar to its 60K label.

      09:00       → 10:00  (session open grouped into first full hour)
      09:01–10:00 → 10:00
      10:01–11:00 → 11:00
      11:01–12:00 → 12:00
      12:01–13:00 → 13:00
      13:01–13:30 → 13:30  (partial last bar)
    """
    t  = ts.time()
    fl = ts.floor("min")

    if not (_SPOT_DAY_START <= t <= _SPOT_DAY_END):
        return pd.NaT

    if dtime(13, 0) < t <= dtime(13, 30):
        return fl.replace(hour=13, minute=30)

    if t.minute == 0 and t.hour != 9:
        return fl
    else:
        return (fl + pd.Timedelta(hours=1)).replace(minute=0)


# ===========================================================================
# Shioaji API singleton
# ===========================================================================

_api: "sj.Shioaji | None" = None


def _get_api() -> "sj.Shioaji":
    global _api
    if _api is None:
        _api = _create_and_login()
    return _api


def init_api() -> None:
    """Eagerly initialize the Shioaji API singleton. Call once at startup."""
    _get_api()


def _create_and_login() -> "sj.Shioaji":
    api_key    = os.getenv("SHIOAJI_API_KEY", "").strip()
    secret_key = os.getenv("SHIOAJI_SECRET_KEY", "").strip()

    if not api_key or not secret_key:
        raise EnvironmentError(
            "SHIOAJI_API_KEY and/or SHIOAJI_SECRET_KEY are missing from .env"
        )

    api = sj.Shioaji()
    try:
        api.login(api_key=api_key, secret_key=secret_key)
        logger.info("Shioaji login successful.")
    except Exception as exc:
        logger.error("Shioaji login failed: %s", exc)
        raise RuntimeError(f"Shioaji login failed: {exc}") from exc

    try:
        api.fetch_contracts()
        logger.info("Shioaji fetch_contracts completed.")
    except Exception as exc:
        logger.warning("fetch_contracts 失敗，將嘗試直接存取合約: %s", exc)

    atexit.register(_logout, api)
    return api


def _logout(api: "sj.Shioaji") -> None:
    try:
        api.logout()
        logger.info("Shioaji logout completed.")
    except Exception as exc:
        logger.warning("Shioaji logout warning: %s", exc)


def _is_token_error(exc: Exception) -> bool:
    """判斷例外是否為 Token 過期 / 401 認證失敗。"""
    if _SjTokenError is not None and isinstance(exc, _SjTokenError):
        return True
    msg = str(exc).lower()
    return "401" in msg or ("token" in msg and "expir" in msg)


def _force_relogin() -> "sj.Shioaji":
    """
    強制重新建立 API 連線（跨週末或長時間閒置後 Token 過期時使用）。

    清除舊的 singleton、嘗試登出舊 session，然後重新執行 login + fetch_contracts。
    """
    global _api
    logger.warning("偵測到 Token 過期或連線中斷，執行自動重新登入...")
    if _api is not None:
        try:
            _api.logout()
        except Exception as exc:
            logger.debug("重登入前舊 session 登出失敗（可忽略）: %s", exc)
    _api = None
    new_api = _get_api()
    logger.info("自動重新登入成功，新 Token 已就緒。")
    return new_api


# ===========================================================================
# Contract pre-warming (確保訂閱狀態為 Active)
# ===========================================================================

def _warm_contract(api: "sj.Shioaji", symbol: str) -> None:
    """
    在 api.kbars() 之前存取合約物件，確保 Shioaji 內部報價訂閱狀態為 Active。

    對 TXFR1 存取 api.Contracts.Futures.TXF.TXFR1；
    對現貨則存取對應的 Index 合約群組。
    失敗時僅記錄 DEBUG，不拋出例外。
    """
    try:
        if symbol == "TXFR1":
            _ = api.Contracts.Futures.TXF.TXFR1
            logger.debug("[%s] 合約預熱完成 (Futures.TXF.TXFR1)", symbol)
        elif symbol in _SPOT_SYMBOLS:
            market, code = _SPOT_SYMBOLS[symbol]
            indexs_group = (
                getattr(api.Contracts, "Indexs", None)
                or getattr(api.Contracts, "Index", None)
                or getattr(api.Contracts, "indexes", None)
            )
            if indexs_group:
                mkt_grp = getattr(indexs_group, market, None)
                if mkt_grp:
                    _ = getattr(mkt_grp, code, None)
                    logger.debug("[%s] 合約預熱完成 (Index.%s.%s)", symbol, market, code)
    except Exception as exc:
        logger.debug("[%s] 合約預熱失敗（非致命）: %s", symbol, exc)


# ===========================================================================
# Data stagnation diagnosis (資料停滯診斷)
# ===========================================================================

_STAGNATION_LAG_MINUTES = 10   # 超過此分鐘數則進行 Snapshot 對比


def _validate_data_freshness(
    api:      "sj.Shioaji",
    symbol:   str,
    df_1min:  pd.DataFrame,
    is_spot:  bool,
) -> None:
    """
    檢查最後一根 K 線時間是否落後當前時間超過 _STAGNATION_LAG_MINUTES 分鐘。

    若落後超標，對 TXFR1 執行 Snapshot 對比：
      邏輯 A — Snapshot 是新的，Kbars 是舊的：
                → [ERROR]   Shioaji Kbars Server Lagging
      邏輯 B — Snapshot 與 Kbars 皆為舊資料（或 Snapshot 取得失敗）：
                → [WARNING] Detection of Data Stagnation. Attempting Session Reset...
                → 立即執行 _force_relogin() 重置 Session

    Debug 日誌（每次抓取皆輸出）：
      [DEBUG] 品種: {symbol} | 最後 K 線時間: {ts} | 系統時間: {now} | 最新成交價: {close}
    """
    now_tw = datetime.now(TW_TZ)

    # ── 取得最後一根 K 線資訊 ──────────────────────────────────────────────
    if df_1min.empty:
        logger.debug(
            "[DEBUG] 品種: %s | 最後 K 線時間: N/A | 系統時間: %s | 最新成交價: N/A",
            symbol, now_tw.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
        return

    if "Datetime" in df_1min.columns:
        last_bar_ts = df_1min["Datetime"].iloc[-1]
    else:
        last_bar_ts = df_1min.index[-1]

    if hasattr(last_bar_ts, "tzinfo") and last_bar_ts.tzinfo is None:
        last_bar_ts = TW_TZ.localize(pd.Timestamp(last_bar_ts))
    else:
        last_bar_ts = pd.Timestamp(last_bar_ts)

    last_close = float(df_1min["Close"].iloc[-1]) if "Close" in df_1min.columns else None

    # ── 必輸出的 DEBUG 行 ─────────────────────────────────────────────────
    logger.debug(
        "[DEBUG] 品種: %s | 最後 K 線時間: %s | 系統時間: %s | 最新成交價: %s",
        symbol,
        last_bar_ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
        now_tw.strftime("%Y-%m-%d %H:%M:%S %Z"),
        f"{last_close:.0f}" if last_close is not None else "N/A",
    )

    lag = now_tw - last_bar_ts
    if lag <= pd.Timedelta(minutes=_STAGNATION_LAG_MINUTES):
        return   # 資料新鮮，無需進一步診斷

    # 現貨僅在日盤期間才有意義，夜間超時不診斷
    if is_spot:
        return

    logger.warning(
        "[%s] 最後 K 線 (%s) 落後系統時間 %s 超過 %d 分鐘，啟動 Snapshot 對比診斷...",
        symbol,
        last_bar_ts.strftime("%H:%M:%S"),
        now_tw.strftime("%H:%M:%S"),
        _STAGNATION_LAG_MINUTES,
    )

    # ── Snapshot 對比 ──────────────────────────────────────────────────────
    snap_ts: "pd.Timestamp | None" = None
    try:
        contract = api.Contracts.Futures.TXF.TXFR1
        snapshots = api.snapshots([contract])
        if snapshots:
            snap = snapshots[0]
            raw_ts = getattr(snap, "ts", None) or getattr(snap, "datetime", None)
            if raw_ts is not None:
                snap_ts = pd.Timestamp(raw_ts)
                if snap_ts.tzinfo is None:
                    snap_ts = snap_ts.tz_localize("Asia/Taipei")
    except Exception as exc:
        logger.warning("[%s] Snapshot 取得失敗: %s", symbol, exc)

    if snap_ts is not None and (now_tw - snap_ts) <= pd.Timedelta(minutes=_STAGNATION_LAG_MINUTES):
        # 邏輯 A：Snapshot 新鮮，但 Kbars 是舊的 → Server Lag
        logger.error(
            "[ERROR] Shioaji Kbars Server Lagging - Kbars Data not matching Snapshots"
            " | Snapshot 最新: %s | Kbars 最後: %s | 落後: %.1f 分鐘",
            snap_ts.strftime("%H:%M:%S"),
            last_bar_ts.strftime("%H:%M:%S"),
            lag.total_seconds() / 60,
        )
    else:
        # 邏輯 B：兩者皆舊（或 Snapshot 失敗）→ Session Expired
        logger.warning(
            "[WARNING] Detection of Data Stagnation. Attempting Session Reset..."
            " | Kbars 最後: %s | Snapshot: %s",
            last_bar_ts.strftime("%H:%M:%S"),
            snap_ts.strftime("%H:%M:%S") if snap_ts is not None else "N/A",
        )
        _force_relogin()


# ===========================================================================
# Data normalisation
# ===========================================================================

def _normalise(kbars) -> pd.DataFrame:
    """
    Convert Shioaji Kbars to a clean 1-min OHLCV DataFrame (TXF futures).
    Filters to TXF session windows and drops zero-volume bars.
    """
    df = pd.DataFrame({**kbars})
    if df.empty:
        return df

    df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing  = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Shioaji kbars response is missing columns: {missing}")

    ts_col = df["ts"]
    if pd.api.types.is_integer_dtype(ts_col):
        dt_index = pd.to_datetime(ts_col, unit="ns").dt.tz_localize("Asia/Taipei")
    else:
        dt_index = pd.to_datetime(ts_col).dt.tz_localize("Asia/Taipei")

    df["Datetime"] = dt_index
    df = df.set_index("Datetime")[required].copy()

    session_mask = pd.Series(df.index.time, index=df.index).apply(
        lambda t: _get_trading_session(t) is not None
    )
    df = df[session_mask.values]
    df = df[df["Volume"] > 0]

    df = df.dropna(subset=["Close"]).sort_index()
    df.index.name = "Datetime"
    return df.reset_index()


def _normalise_spot(kbars) -> pd.DataFrame:
    """
    Convert Shioaji Kbars to a clean 1-min OHLCV DataFrame (spot index).
    Filters to 09:00–13:30 session.
    """
    df = pd.DataFrame({**kbars})
    if df.empty:
        return df

    df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing  = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Shioaji kbars response is missing columns: {missing}")

    ts_col = df["ts"]
    if pd.api.types.is_integer_dtype(ts_col):
        dt_index = pd.to_datetime(ts_col, unit="ns").dt.tz_localize("Asia/Taipei")
    else:
        dt_index = pd.to_datetime(ts_col).dt.tz_localize("Asia/Taipei")

    df["Datetime"] = dt_index
    df = df.set_index("Datetime")[required].copy()

    session_mask = pd.Series(df.index.time, index=df.index).apply(
        lambda t: _get_spot_trading_session(t) is not None
    )
    df = df[session_mask.values]

    if df["Volume"].sum() > 0:
        df = df[df["Volume"] > 0]

    df = df.dropna(subset=["Close"]).sort_index()
    df.index.name = "Datetime"
    return df.reset_index()


# ===========================================================================
# Resample functions
# ===========================================================================

def _resample_session_aware(df_1min: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Resample TXF 1-min OHLCV to *timeframe* with session isolation.

    Uses offset='45min' to align bins to TXF's 08:45 open.
    """
    df = df_1min.copy()
    if "Datetime" in df.columns:
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    session_mask = pd.Series(df.index.time, index=df.index).apply(
        lambda t: _get_trading_session(t) is not None
    )
    df = df[session_mask.values]
    if df.empty:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    agg_rules = {
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }

    time_diffs = df.index.to_series().diff()
    new_block  = (time_diffs > _SESSION_GAP) | time_diffs.isna()
    df["_block"] = new_block.cumsum()

    rs_kwargs: dict = {"closed": "right", "label": "right"}
    if _PD_GTE_1_1:
        rs_kwargs["offset"] = "45min"
    else:
        rs_kwargs["base"] = 45

    blocks: list[pd.DataFrame] = []
    for _, block_df in df.groupby("_block", sort=True):
        resampled = (
            block_df.drop(columns=["_block"])
            .resample(timeframe, **rs_kwargs)
            .agg(agg_rules)
        )
        resampled = resampled[resampled["Volume"] > 0].dropna(how="all")
        if not resampled.empty:
            blocks.append(resampled)

    if not blocks:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    df_out = pd.concat(blocks).sort_index()
    df_out.index.name = "Datetime"
    return df_out.reset_index()


def _resample_spot(df_1min: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample spot index 1-min OHLCV to *timeframe* (standard bins)."""
    df = df_1min.copy()
    if "Datetime" in df.columns:
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    if df.empty:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    agg_rules = {
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }

    time_diffs = df.index.to_series().diff()
    new_block  = (time_diffs > _SESSION_GAP) | time_diffs.isna()
    df["_block"] = new_block.cumsum()

    rs_kwargs: dict = {"closed": "right", "label": "right"}

    blocks: list[pd.DataFrame] = []
    for _, block_df in df.groupby("_block", sort=True):
        resampled = (
            block_df.drop(columns=["_block"])
            .resample(timeframe, **rs_kwargs)
            .agg(agg_rules)
        )
        resampled = resampled.dropna(how="all")
        if df["Volume"].sum() > 0:
            resampled = resampled[resampled["Volume"] > 0]
        if not resampled.empty:
            blocks.append(resampled)

    if not blocks:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    df_out = pd.concat(blocks).sort_index()
    df_out.index.name = "Datetime"
    return df_out.reset_index()


def _resample_60min(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Resample TXF 1-min OHLCV to 60-min using XQ's TXF-specific cut points.

    Day session: cut at :46 (bins labelled :45)
    Night session: cut at :01 (bins labelled :00)
    """
    df = df_1min.copy()
    if "Datetime" in df.columns:
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    agg_rules = {
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }

    sessions = pd.Series(
        [_get_trading_session(ts.time()) for ts in df.index],
        index=df.index,
    )
    labels = df.index.map(_get_60k_label)

    valid    = sessions.notna() & labels.notna()
    df       = df[valid]
    sessions = sessions[valid]
    labels   = labels[valid]

    if df.empty:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    df = df.copy()
    df["_session"] = sessions.values
    df["_label"]   = labels.values

    df_out = (
        df.groupby(["_session", "_label"])
        .agg(agg_rules)
        .dropna(how="all")
    )
    df_out = df_out[df_out["Volume"] > 0]
    df_out = df_out.reset_index(level="_session", drop=True)
    df_out.index.name = "Datetime"
    return df_out.sort_index().reset_index()


def _resample_60min_spot(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Resample spot index 1-min OHLCV to 60-min (spot-specific cut points).

    09:00–10:00 → 10:00,  10:01–11:00 → 11:00,  …,  13:01–13:30 → 13:30
    """
    df = df_1min.copy()
    if "Datetime" in df.columns:
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    agg_rules = {
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }

    labels = df.index.map(_get_60k_label_spot)
    valid  = labels.notna()
    df     = df[valid]
    labels = labels[valid]

    if df.empty:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    df = df.copy()
    df["_label"] = labels.values

    df_out = (
        df.groupby("_label")
        .agg(agg_rules)
        .dropna(how="all")
    )
    if df["Volume"].sum() > 0:
        df_out = df_out[df_out["Volume"] > 0]

    df_out.index.name = "Datetime"
    return df_out.sort_index().reset_index()


def _resample_spot_to_daily(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate spot 1-min OHLCV bars into one daily bar per trading day.

    Each day's 09:00–13:30 bars collapse into a single OHLCV bar whose
    index timestamp is midnight of that calendar date.

    Parameters
    ----------
    df_1min : Output of _normalise_spot() — "Datetime" column, tz-aware
              Asia/Taipei, already filtered to spot session hours.

    Returns
    -------
    DataFrame with columns: Datetime (tz-aware Asia/Taipei midnight),
    Open, High, Low, Close, Volume.
    """
    df = df_1min.copy()
    if "Datetime" in df.columns:
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    # normalize() floors tz-aware timestamps to midnight in their tz,
    # so all 09:00–13:30 bars from the same trading date share one key.
    date_keys = df.index.normalize()

    df_daily = df.groupby(date_keys).agg(
        Open=("Open",    "first"),
        High=("High",    "max"),
        Low=("Low",      "min"),
        Close=("Close",  "last"),
        Volume=("Volume","sum"),
    )
    df_daily.index.name = "Datetime"
    return df_daily.reset_index()


# ===========================================================================
# MA computation helper
# ===========================================================================

def _compute_and_attach_ma(df: pd.DataFrame, symbol: str, timeframe: str) -> pd.DataFrame:
    """
    Compute MA5/10/20/60/240 on *df* (indexed by Datetime) and attach them
    as new columns.  The computation is performed on the FULL df so tail
    values are accurate — callers should pass the complete buffer dataset,
    not the display-window slice.

    Logs a WARNING for each MA period whose result is entirely NaN (i.e.
    fewer available bars than the period requires).  Does NOT raise.

    Returns the same df with MA columns added in-place (also returned for
    convenience in a pipeline).
    """
    n = len(df)
    for period in _MA_PERIODS_COMPUTE:
        col = f"MA{period}"
        df[col] = df["Close"].rolling(period).mean()
        valid = int(df[col].notna().sum())
        if valid == 0:
            logger.warning(
                "[%s] %s: MA%d 全為 NaN — 僅有 %d 根資料，至少需要 %d 根。",
                symbol, timeframe, period, n, period,
            )
        elif n < _MA_BUFFER_PER_TF.get(timeframe, _MA_COMPUTE_MIN_BARS) and period == max(_MA_PERIODS_COMPUTE):
            target = _MA_BUFFER_PER_TF.get(timeframe, _MA_COMPUTE_MIN_BARS)
            logger.warning(
                "[%s] %s: 僅取得 %d 根（目標 %d 根），MA%d 末端可能不準確。",
                symbol, timeframe, n, target, period,
            )
    return df


# ===========================================================================
# Public class
# ===========================================================================

class ShioajiDataFetcher:
    """
    Fetches K-line data from Shioaji for TXFR1, TSE/001, OTC/101.

    Key behaviour change vs. earlier version
    -----------------------------------------
    fetch_bars() now:
      1. Fetches fetch_count = max(bars, _MA_BUFFER_PER_TF[timeframe]) bars.
         (1day→300, 60min→500, 5min→300 — ensures MA240 is non-NaN at tail)
      2. Computes MA5/10/20/60/240 on those fetch_count bars.
      3. Slices to the last *bars* rows.
      4. Returns the slice WITH MA columns attached.

    Callers (renderer.py) receive a DataFrame that already contains correct
    MA values for the display window.  renderer._prepare_and_slice() should
    detect the pre-computed MA columns and skip recomputation.
    """

    def __init__(self, symbol: str = "TXFR1") -> None:
        self._symbol      = symbol
        self._is_spot     = symbol in _SPOT_SYMBOLS
        self._latest_close: "float | None" = None

    # ------------------------------------------------------------------
    # Primary public method
    # ------------------------------------------------------------------

    def fetch_bars(
        self,
        timeframe: str = "5min",
        bars:      int  = 90,
        start:     "str | None" = None,
        end:       "str | None" = None,
    ) -> pd.DataFrame:
        """
        Fetch K-line bars at *timeframe* resolution with pre-computed MAs.

        Parameters
        ----------
        timeframe : One of "1min", "5min", "10min", "15min", "30min",
                    "60min", "1day".
                    "1day" is only valid for spot symbols (TSE/OTC).
        bars      : Number of display bars to return (most recent N).
                    Internally, max(bars, _MA_BUFFER_PER_TF[timeframe]) bars
                    are fetched so that MA240 (年線) is always calculable:
                      "1day"  → buffer 300 bars  (display 45)
                      "60min" → buffer 500 bars  (display 65)
                      "5min"  → buffer 300 bars  (display 90)
        start     : Date string "yyyy-mm-dd".  Auto-derived if omitted.
        end       : Date string "yyyy-mm-dd".  Defaults to today.

        Returns
        -------
        pd.DataFrame with columns:
          Datetime (tz-aware Asia/Taipei), Open, High, Low, Close, Volume,
          MA5, MA10, MA20, MA60, MA240.

        MA values are computed on the full internal buffer before slicing,
        so even MA240 is non-NaN at the tail of the display window.
        """
        supported = set(_TIMEFRAME_MIN.keys())
        if timeframe not in supported:
            raise ValueError(
                f"Unsupported timeframe '{timeframe}'. "
                f"Supported: {sorted(supported)}"
            )

        if timeframe == "1day" and not self._is_spot:
            raise ValueError(
                f"'1day' 時框僅支援現貨品種 (TSE/001, OTC/101)。"
                f"  品種 {self._symbol} 為期貨，請使用 '5min' 或 '60min'。"
            )

        min_per_bar = _TIMEFRAME_MIN[timeframe]

        # ── Buffer size for MA computation ──────────────────────────────────
        # Use the per-timeframe minimum defined in _MA_BUFFER_PER_TF so that
        # MA240 has a valid (non-NaN) tail value even for small display windows:
        #   1day  → 300 bars   60min → 500 bars   5min → 300 bars
        tf_min_bars = _MA_BUFFER_PER_TF.get(timeframe, _MA_COMPUTE_MIN_BARS)
        fetch_count = max(bars, tf_min_bars)
        needed_1min = int(fetch_count * min_per_bar * 1.5)

        # ── Date range（強制使用 Asia/Taipei 時區計算）──────────────────────
        # 以 TW_TZ 當前時間為基準，確保跨午夜的夜盤資料不會因 UTC 日期偏移而漏抓。
        # end_date  = 台北今天（不晚於明天）
        # start_date = 最少回溯 2 天（保證夜盤完整），再依 MA buffer 往前延伸。
        now_tw = datetime.now(TW_TZ)
        today  = now_tw.date()

        if end is None:
            end = today.strftime("%Y-%m-%d")
        if start is None:
            mins_per_day = _SPOT_TRADING_MIN_PER_DAY if self._is_spot else _TRADING_MIN_PER_DAY
            trading_days = math.ceil(needed_1min / mins_per_day)
            lookback     = max(math.ceil(trading_days * 7 / 5) + 10, 2)   # 最少 2 天
            start = (today - timedelta(days=lookback)).strftime("%Y-%m-%d")

        logger.info(
            "Fetching 1-min kbars: %s  %s → %s  "
            "(target: %d %s display bars, fetch buffer: %d bars, ~%d 1-min bars)",
            self._symbol, start, end,
            bars, timeframe, fetch_count, needed_1min,
        )

        # ── API call with Token-expiry retry ────────────────────────────────
        # Up to 2 re-login attempts to handle cross-weekend invalidation and
        # occasional double-expiry on long-running processes.
        _MAX_RELOGIN = 2
        kbars = None
        for _attempt in range(_MAX_RELOGIN + 1):
            api      = _get_api()
            contract = self._get_contract(api)

            # ── 合約預熱：確保報價訂閱狀態為 Active ──────────────────────
            _warm_contract(api, self._symbol)

            try:
                kbars = api.kbars(contract, start=start, end=end)
                break
            except Exception as exc:
                exc_str = str(exc).lower()

                if _is_token_error(exc) and _attempt < _MAX_RELOGIN:
                    logger.warning(
                        "[%s] Token 過期 (401)，嘗試第 %d/%d 次自動重新登入...",
                        self._symbol, _attempt + 1, _MAX_RELOGIN,
                    )
                    _force_relogin()
                    continue

                if "permission" in exc_str or "unauthorized" in exc_str or "403" in exc_str:
                    logger.error(
                        "資料權限尚未開通！(%s)\n原始錯誤: %s", self._symbol, exc
                    )
                    raise PermissionError(
                        f"Shioaji 資料權限不足 ({self._symbol}): {exc}"
                    ) from exc

                logger.error("api.kbars() raised: %s", exc)
                raise RuntimeError(
                    f"Shioaji kbars fetch failed ({self._symbol}): {exc}"
                ) from exc

        # ── Normalise to 1-min DataFrame ────────────────────────────────────
        if self._is_spot:
            df_1min = _normalise_spot(kbars)
        else:
            df_1min = _normalise(kbars)

        if df_1min.empty:
            raise ValueError(
                f"No kbar data returned for {self._symbol} ({start} → {end})"
            )

        logger.info(
            "Fetched %d 1-min bars from exchange (%s).",
            len(df_1min), self._symbol,
        )

        # ── 資料停滯診斷（三層防禦）─────────────────────────────────────────
        # 比較最後一根 1-min K 線時間與系統時間；超過 10 分鐘則進行
        # Snapshot 對比，自動區分 Server Lag（邏輯 A）與 Session 失效（邏輯 B）。
        _validate_data_freshness(api, self._symbol, df_1min, self._is_spot)

        # ── Resample ─────────────────────────────────────────────────────────
        if timeframe == "1min":
            df_out = df_1min

        elif timeframe == "1day":
            df_out = _resample_spot_to_daily(df_1min)
            logger.info(
                "Daily resample (%s): %d bars (from %d 1-min bars)",
                self._symbol, len(df_out), len(df_1min),
            )

        elif timeframe == "60min":
            if self._is_spot:
                df_out = _resample_60min_spot(df_1min)
            else:
                df_out = _resample_60min(df_1min)
            logger.info(
                "60K resample (%s): %d bars (from %d 1-min bars)",
                self._symbol, len(df_out), len(df_1min),
            )

        else:
            if self._is_spot:
                df_out = _resample_spot(df_1min, timeframe)
            else:
                df_out = _resample_session_aware(df_1min, timeframe)
            logger.info(
                "Session-aware resample → %s (%s): %d bars (from %d 1-min bars)",
                timeframe, self._symbol, len(df_out), len(df_1min),
            )

        if df_out.empty:
            raise ValueError(
                f"Resample to {timeframe} produced no bars for {self._symbol}"
            )

        # ── MA pre-computation on full buffer ────────────────────────────────
        # Set DatetimeIndex for rolling() to work correctly, compute MAs,
        # then reset_index() so "Datetime" is a column again (consistent with
        # the rest of the codebase).
        if "Datetime" in df_out.columns:
            df_out = df_out.set_index("Datetime")

        _compute_and_attach_ma(df_out, self._symbol, timeframe)

        df_out = df_out.reset_index()   # Datetime back as column

        # ── Slice to display window ──────────────────────────────────────────
        # MA is already correct at the tail; now trim to the requested bars.
        if len(df_out) > bars:
            df_out = df_out.iloc[-bars:].reset_index(drop=True)

        if df_out.empty:
            raise ValueError(
                f"No bars remaining after MA + slice for {self._symbol} ({timeframe})"
            )

        self._latest_close = float(df_out["Close"].iloc[-1])

        # ── 最終 DEBUG 彙總（無論成功與否皆輸出）─────────────────────────
        _last_ts   = df_out["Datetime"].iloc[-1] if "Datetime" in df_out.columns else "N/A"
        _now_final = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        _ts_str    = (
            _last_ts.strftime("%Y-%m-%d %H:%M:%S %Z")
            if hasattr(_last_ts, "strftime") else str(_last_ts)
        )
        logger.debug(
            "[DEBUG] 品種: %s | 最後 K 線時間: %s | 系統時間: %s | 最新成交價: %.0f",
            self._symbol, _ts_str, _now_final, self._latest_close,
        )
        logger.info(
            "fetch_bars(%s, %s): returning %d bars (MA pre-computed) | "
            "latest close = %.0f",
            self._symbol, timeframe, len(df_out), self._latest_close,
        )
        return df_out

    def get_latest_price(self) -> float:
        if self._latest_close is None:
            df = self.fetch_bars("5min", bars=2)
            return float(df["Close"].iloc[-1])
        return self._latest_close

    # ------------------------------------------------------------------
    # Contract lookup
    # ------------------------------------------------------------------

    def _get_contract(self, api: "sj.Shioaji"):
        if self._is_spot:
            return self._get_spot_contract(api)
        return self._get_futures_contract(api)

    def _get_spot_contract(self, api: "sj.Shioaji"):
        """Locate TSE/001 or OTC/101 index contract in Shioaji."""
        market, code = _SPOT_SYMBOLS[self._symbol]

        indexs_group = None
        for attr in ("Indexs", "Index", "indexes"):
            indexs_group = getattr(api.Contracts, attr, None)
            if indexs_group is not None:
                break

        if indexs_group is None:
            raise RuntimeError(
                f"無法找到 Index 合約群組。\n"
                f"  請確認 Shioaji 版本並已成功 fetch_contracts()。\n"
                f"  Symbol: {self._symbol}"
            )

        market_group = getattr(indexs_group, market, None)
        if market_group is None:
            available = [a for a in dir(indexs_group) if not a.startswith("_")]
            raise RuntimeError(
                f"無法找到 {market} 在 Index 合約群組中。\n"
                f"  可用群組: {available}"
            )

        contract = getattr(market_group, code, None)
        if contract is None:
            available = [a for a in dir(market_group) if not a.startswith("_")]
            raise RuntimeError(
                f"無法找到合約 {code} 在 {market} 群組中。\n"
                f"  可用合約: {available}"
            )
        return contract

    @staticmethod
    def _get_futures_contract(api: "sj.Shioaji"):
        """Locate TXFR1 futures contract in Shioaji."""
        txf_group = getattr(api.Contracts.Futures, "TXF", None)
        contract  = getattr(txf_group, "TXFR1", None) if txf_group is not None else None

        if contract is None:
            available    = dir(txf_group) if txf_group is not None else []
            futures_hint = [a for a in available if a.startswith("TXF")]
            raise RuntimeError(
                "無法取得 TXFR1 合約物件。\n"
                f"  api.Contracts.Futures.TXF 下可見的合約: "
                f"{futures_hint or '(空，可能需要先呼叫 fetch_contracts)'}\n"
                "  請確認 Shioaji 版本並已成功登入。"
            )
        return contract


# ===========================================================================
# Backwards-compatible alias
# ===========================================================================

YFinanceDataFetcher = ShioajiDataFetcher


# ===========================================================================
# Standalone convenience function
# ===========================================================================

def fetch_data(
    symbol:    str  = "TXFR1",
    timeframe: str  = "5min",
    bars:      int  = 90,
    start:     "str | None" = None,
    end:       "str | None" = None,
) -> pd.DataFrame:
    """Fetch K-line data for *symbol* at the given resolution (with MA columns)."""
    return ShioajiDataFetcher(symbol).fetch_bars(
        timeframe=timeframe, bars=bars, start=start, end=end
    )
