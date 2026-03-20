"""
fetcher.py — Data fetching for TX-Observer.

Uses Shioaji (永豐金 API) to fetch 台指期貨近一連續合約 (TXFR1) K-line data
directly at the target resolution (5-min or 60-min) via api.kbars(unit=...),
eliminating manual resampling and the associated rounding errors.

Public interface
----------------
fetcher = ShioajiDataFetcher()
df_5k  = fetcher.fetch_bars("5min",  bars=200)
df_60k = fetcher.fetch_bars("60min", bars=100)

A module-level Shioaji API singleton is created on first use and logged out
automatically via atexit when the process exits.
"""

import atexit
import logging
import math
import os
from datetime import date, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import pytz
import shioaji as sj
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logger = logging.getLogger("tx_observer.fetcher")

TW_TZ = pytz.timezone("Asia/Taipei")


# ---------------------------------------------------------------------------
# TXF session windows (UTC+8 wall-clock times)
# ---------------------------------------------------------------------------
# Day session   : 08:45 – 13:45
# Night session : 15:00 – 05:00 (next day)
# Closed        : 05:01 – 08:44  and  13:46 – 14:59
_DAY_START   = dtime(8, 45)
_DAY_END     = dtime(13, 45)
_NIGHT_START = dtime(15, 0)
_NIGHT_END   = dtime(5, 0)


def _is_in_session(t: dtime) -> bool:
    """Return True if bar timestamp *t* falls within a TXF trading session."""
    if _DAY_START <= t <= _DAY_END:   # day session
        return True
    if t >= _NIGHT_START:             # night session same-day part (15:00–23:59)
        return True
    if t <= _NIGHT_END:               # night session next-day tail (00:00–05:00)
        return True
    return False


# ---------------------------------------------------------------------------
# Timeframe → Shioaji Unit mapping
# ---------------------------------------------------------------------------
_UNIT_MAP: "dict[str, sj.constant.Unit]" = {
    "1min":  sj.constant.Unit.Min1,
    "5min":  sj.constant.Unit.Min5,
    "10min": sj.constant.Unit.Min10,
    "15min": sj.constant.Unit.Min15,
    "30min": sj.constant.Unit.Min30,
    "60min": sj.constant.Unit.Min60,
    "day":   sj.constant.Unit.Day,
}

# Conservative estimate of tradeable minutes per day for TXF
# (day 300 min + average night tail ~180 min = 480)
_TRADING_MIN_PER_DAY = 480


def _lookback_days(timeframe: str, bars: int) -> int:
    """
    Calculate how many calendar days to look back to cover *bars* bars
    at *timeframe* resolution, with a 10-day buffer for holidays.
    """
    if "min" in timeframe:
        min_per_bar = int(timeframe.replace("min", ""))
    else:
        min_per_bar = 1440  # "day"

    trading_days = math.ceil(bars * min_per_bar / _TRADING_MIN_PER_DAY)
    return math.ceil(trading_days * 7 / 5) + 10


# ===========================================================================
# Shioaji API singleton
# ===========================================================================

_api: "sj.Shioaji | None" = None


def _get_api() -> "sj.Shioaji":
    """Return the module-level Shioaji API instance, creating it on first call."""
    global _api
    if _api is None:
        _api = _create_and_login()
    return _api


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

    # Sync contract list so that Contracts.Futures.TXF.TXFR1 is available
    try:
        api.fetch_contracts(contract_type=sj.constant.ContractType.Future)
        logger.info("Shioaji fetch_contracts (Futures) completed.")
    except Exception as exc:
        logger.warning("fetch_contracts 失敗，合約屬性可能不完整: %s", exc)

    atexit.register(_logout, api)
    return api


def _logout(api: "sj.Shioaji") -> None:
    try:
        api.logout()
        logger.info("Shioaji logout completed.")
    except Exception as exc:
        logger.warning("Shioaji logout warning: %s", exc)


# ===========================================================================
# Internal normaliser (module-level so it can be unit-tested independently)
# ===========================================================================

def _normalise(kbars) -> pd.DataFrame:
    """
    Convert a Shioaji Kbars object (any unit) to a clean OHLCV DataFrame.

    Steps
    -----
    1. Unpack kbars → DataFrame
    2. Normalise column names to Title-Case (Open, High, Low, Close, Volume)
    3. ts → tz-aware Asia/Taipei DatetimeIndex
       Shioaji encodes ts as nanoseconds of CST wall-clock time (not UTC),
       so we localise directly to Asia/Taipei without any UTC conversion.
    4. Filter to TXF session windows (day 08:45–13:45, night 15:00–05:00)
    5. Drop zero-volume placeholder bars
    6. Sort ascending, promote index to 'Datetime' column
    """
    df = pd.DataFrame({**kbars})

    if df.empty:
        return df

    # ── Step 2 ────────────────────────────────────────────────────────────
    df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={
        "open":   "Open",
        "high":   "High",
        "low":    "Low",
        "close":  "Close",
        "volume": "Volume",
    })

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing  = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Shioaji kbars response is missing columns: {missing}")

    # ── Step 3: ts → tz-aware Asia/Taipei ────────────────────────────────
    ts_col = df["ts"]
    if pd.api.types.is_integer_dtype(ts_col):
        dt_index = pd.to_datetime(ts_col, unit="ns").dt.tz_localize("Asia/Taipei")
    else:
        dt_index = pd.to_datetime(ts_col).dt.tz_localize("Asia/Taipei")

    df["Datetime"] = dt_index
    df = df.set_index("Datetime")[required].copy()

    # ── Step 4: session filter ────────────────────────────────────────────
    bar_times    = df.index.time
    session_mask = pd.Series(bar_times, index=df.index).apply(_is_in_session)
    df = df[session_mask.values]

    # ── Step 5: drop zero-volume placeholders ─────────────────────────────
    df = df[df["Volume"] > 0]

    # ── Step 6 ────────────────────────────────────────────────────────────
    df = df.dropna(subset=["Close"]).sort_index()
    df.index.name = "Datetime"
    return df.reset_index()


# ===========================================================================
# Public class
# ===========================================================================

class ShioajiDataFetcher:
    """
    Fetches 台指期貨近一連續合約 (TXFR1) K-line data from Shioaji.

    Bars are fetched directly at the target resolution using api.kbars(unit=...),
    eliminating manual resampling and the associated boundary errors.

    Usage
    -----
    fetcher = ShioajiDataFetcher()
    df_5k  = fetcher.fetch_bars("5min",  bars=200)
    df_60k = fetcher.fetch_bars("60min", bars=100)
    """

    _CONTRACT_CODE = "TXFR1"

    def __init__(self, symbol: str = _CONTRACT_CODE) -> None:
        # symbol kept for display/logging; contract lookup always uses TXFR1
        self._symbol       = symbol
        self._latest_close: "float | None" = None

    # ------------------------------------------------------------------
    # Primary public method
    # ------------------------------------------------------------------

    def fetch_bars(
        self,
        timeframe: str = "5min",
        bars:      int  = 200,
        start:     "str | None" = None,
        end:       "str | None" = None,
    ) -> pd.DataFrame:
        """
        Fetch K-line bars at the specified resolution directly from Shioaji.

        Parameters
        ----------
        timeframe : str
            One of "1min", "5min", "10min", "15min", "30min", "60min", "day".
        bars : int
            Maximum number of bars to return (most recent N).
            The API always returns all bars in the date window; we slice
            to the last *bars* rows after normalising.
        start : str, optional
            Date string "yyyy-mm-dd". Auto-derived from *bars* if omitted.
        end : str, optional
            Date string "yyyy-mm-dd". Defaults to today.

        Returns
        -------
        pd.DataFrame
            Columns: Datetime (tz-aware Asia/Taipei), Open, High, Low,
            Close, Volume.

        Raises
        ------
        ValueError       : unsupported timeframe or empty response.
        PermissionError  : account lacks futures data permissions.
        RuntimeError     : login or API call failure.
        """
        if timeframe not in _UNIT_MAP:
            raise ValueError(
                f"Unsupported timeframe '{timeframe}'. "
                f"Supported: {list(_UNIT_MAP)}"
            )

        today = date.today()
        if end is None:
            end = today.strftime("%Y-%m-%d")
        if start is None:
            start = (
                today - timedelta(days=_lookback_days(timeframe, bars))
            ).strftime("%Y-%m-%d")

        api      = _get_api()
        unit     = _UNIT_MAP[timeframe]
        contract = self._get_contract(api)

        logger.info(
            "Fetching %s kbars: TXFR1  %s → %s  (want last %d bars)",
            timeframe, start, end, bars,
        )

        try:
            kbars = api.kbars(contract, start=start, end=end, unit=unit)
        except Exception as exc:
            exc_str = str(exc).lower()
            if "permission" in exc_str or "unauthorized" in exc_str or "403" in exc_str:
                logger.error(
                    "❌ 期貨資料權限尚未開通！\n"
                    "   請至永豐金證券後台確認帳號已申請「期貨行情資料」權限。\n"
                    "   原始錯誤: %s",
                    exc,
                )
                raise PermissionError(
                    f"Shioaji 期貨資料權限不足 (Permission Denied): {exc}"
                ) from exc
            logger.error("api.kbars() raised: %s", exc)
            raise RuntimeError(f"Shioaji kbars fetch failed: {exc}") from exc

        df = _normalise(kbars)

        if df.empty:
            raise ValueError(
                f"No kbar data returned for TXFR1 {timeframe} ({start} → {end})"
            )

        # Slice to the most recent N bars
        if len(df) > bars:
            df = df.iloc[-bars:].reset_index(drop=True)

        self._latest_close = float(df["Close"].iloc[-1])
        logger.info(
            "fetch_bars(%s): got %d bars | TXFR1 | latest close = %.0f",
            timeframe, len(df), self._latest_close,
        )
        return df

    def get_latest_price(self) -> float:
        """Return the most recently fetched close price."""
        if self._latest_close is None:
            df = self.fetch_bars("5min", bars=2)
            return float(df["Close"].iloc[-1])
        return self._latest_close

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_contract(api: "sj.Shioaji"):
        """Locate the TXFR1 contract object with a descriptive error on failure."""
        txf_group = getattr(api.Contracts.Futures, "TXF", None)
        contract  = getattr(txf_group, "TXFR1", None) if txf_group is not None else None

        if contract is None:
            available    = dir(txf_group) if txf_group is not None else []
            futures_hint = [a for a in available if a.startswith("TXF")]
            raise RuntimeError(
                "無法取得 TXFR1 合約物件。\n"
                f"  api.Contracts.Futures.TXF 下可見的合約: "
                f"{futures_hint or '(空，可能需要先呼叫 fetch_contracts)'}\n"
                "  請確認 Shioaji 版本 (>= 1.1) 並已呼叫 api.fetch_contracts()。"
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
    timeframe: str = "5min",
    bars:      int  = 200,
    start:     "str | None" = None,
    end:       "str | None" = None,
) -> pd.DataFrame:
    """
    Convenience wrapper — fetch TXFR1 K-line data at the given resolution.

    Parameters
    ----------
    timeframe : str   "1min", "5min", "10min", "15min", "30min", "60min", "day".
    bars      : int   Maximum bars to return (most recent N).
    start     : str   Date string "yyyy-mm-dd" (optional).
    end       : str   Date string "yyyy-mm-dd" (optional).
    """
    return ShioajiDataFetcher().fetch_bars(
        timeframe=timeframe, bars=bars, start=start, end=end
    )
