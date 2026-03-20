"""
fetcher.py — Data fetching and resampling for TX-Observer.

Uses Shioaji (永豐金 API) to fetch 台指期貨近月連續合約 (TXF00) 1-minute
OHLCV bars, covering both the day session (08:45–13:45) and the night session
(15:00–next-day 05:00).

Public interface is kept identical to the original so main.py needs minimal
changes (YFinanceDataFetcher is aliased to ShioajiDataFetcher).

A module-level Shioaji API singleton is created on first use and logged out
automatically via atexit when the process exits.
"""

import atexit
import logging
import os
from datetime import date, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import pytz
import shioaji as sj
from dotenv import load_dotenv

# Use the same .env path resolution as config.py (absolute, not CWD-relative)
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
_NIGHT_END   = dtime(5, 0)   # inclusive — bars at exactly 05:00 are kept


def _is_in_session(t: dtime) -> bool:
    """Return True if bar time *t* falls within a TXF trading session."""
    # Day session
    if _DAY_START <= t <= _DAY_END:
        return True
    # Night session — same day part (15:00–23:59)
    if t >= _NIGHT_START:
        return True
    # Night session — next-day part (00:00–05:00)
    if t <= _NIGHT_END:
        return True
    return False


# ---------------------------------------------------------------------------
# Default lookback
# ---------------------------------------------------------------------------
# TXF trades ~480 min/day on average (day 300 min + night tail).
# MA240 on 60K requires 240 hourly bars ≈ 14,400 1-min bars.
# 14,400 / 480 = 30 trading days → ~42 calendar days + 7-day buffer = 50 days.
# We round up to 50 to be safe.
_DEFAULT_LOOKBACK_DAYS = 50


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

    atexit.register(_logout, api)
    return api


def _logout(api: "sj.Shioaji") -> None:
    try:
        api.logout()
        logger.info("Shioaji logout completed.")
    except Exception as exc:
        logger.warning("Shioaji logout warning: %s", exc)


# ===========================================================================
# Public class
# ===========================================================================

class ShioajiDataFetcher:
    """
    Fetches 台指期貨近月連續合約 (TXF00) 1-minute OHLCV bars from Shioaji.

    Usage
    -----
    fetcher = ShioajiDataFetcher()
    df_1min = fetcher.fetch_1min_bars()
    df_5k   = ShioajiDataFetcher.resample_to_timeframe(df_1min, "5min")
    df_60k  = ShioajiDataFetcher.resample_to_timeframe(df_1min, "60min")
    """

    # Contract code for 台指期近月連續合約
    _CONTRACT_CODE = "TXF00"

    def __init__(self, symbol: str = _CONTRACT_CODE) -> None:
        # symbol is kept for display / logging; contract lookup always uses TXF00
        self._symbol = symbol
        self._latest_close: "float | None" = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_1min_bars(
        self,
        periods: int = 20_000,
        start:   "str | None" = None,
        end:     "str | None" = None,
    ) -> pd.DataFrame:
        """
        Fetch the most recent 1-minute bars for TXF00.

        Parameters
        ----------
        periods : int
            Soft cap on the number of bars returned (most recent N bars).
            The actual fetch window is determined by *start* / *end*.
        start : str, optional
            Date string "yyyy-mm-dd".  Defaults to today - _DEFAULT_LOOKBACK_DAYS.
        end : str, optional
            Date string "yyyy-mm-dd".  Defaults to today.

        Returns
        -------
        pd.DataFrame
            Columns: Datetime (tz-aware Asia/Taipei), Open, High, Low, Close, Volume

        Raises
        ------
        RuntimeError
            If Shioaji login or kbars call fails.
        PermissionError
            If the account does not have futures data permissions.
        ValueError
            If the response contains no rows.
        """
        today = date.today()
        if end is None:
            end = today.strftime("%Y-%m-%d")
        if start is None:
            start = (today - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

        api = _get_api()

        # ── Locate the contract ──────────────────────────────────────────
        try:
            contract = api.Contracts.Futures.TXF.TXF00
        except AttributeError as exc:
            raise RuntimeError(
                "無法取得 TXF00 合約物件。請確認 Shioaji 版本與帳號期貨資料權限。"
            ) from exc

        logger.info(
            "Fetching 1-min kbars: TXF00 (台指期近月連續合約)  %s → %s", start, end
        )

        # ── Call kbars ──────────────────────────────────────────────────
        try:
            kbars = api.kbars(contract, start=start, end=end)
        except Exception as exc:
            exc_str = str(exc).lower()
            if "permission" in exc_str or "unauthorized" in exc_str or "403" in exc_str:
                logger.error(
                    "❌ 期貨資料權限尚未開通！\n"
                    "   請至永豐金證券後台確認帳號已申請「期貨行情資料」權限，\n"
                    "   或聯繫客服開通 API 期貨報價功能。\n"
                    "   原始錯誤: %s",
                    exc,
                )
                raise PermissionError(
                    f"Shioaji 期貨資料權限不足 (Permission Denied): {exc}"
                ) from exc
            logger.error("api.kbars() raised an exception: %s", exc)
            raise RuntimeError(f"Shioaji kbars fetch failed: {exc}") from exc

        df = self._normalise(kbars)

        if df.empty:
            logger.error(
                "api.kbars() returned no data for TXF00 (%s → %s). "
                "市場可能休市，或合約在此區間無成交資料。",
                start, end,
            )
            raise ValueError(f"No kbar data returned for TXF00 ({start} → {end})")

        # Apply soft cap (keep the most recent N bars)
        if len(df) > periods:
            df = df.iloc[-periods:].reset_index(drop=True)

        self._latest_close = float(df["Close"].iloc[-1])
        logger.info(
            "ShioajiDataFetcher: %d 1-min bars | TXF00 | latest close = %.0f",
            len(df), self._latest_close,
        )

        # ── Continuity check ────────────────────────────────────────────
        self._check_continuity(df)

        return df

    def get_latest_price(self) -> float:
        """Return the most recently fetched close price."""
        if self._latest_close is None:
            df = self.fetch_1min_bars(periods=2)
            return float(df["Close"].iloc[-1])
        return self._latest_close

    # ------------------------------------------------------------------
    # Resampling (static — works with any OHLCV DataFrame)
    # ------------------------------------------------------------------

    @staticmethod
    def resample_to_timeframe(df_1min: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """
        Resample 1-minute OHLCV data to a coarser timeframe.

        Parameters
        ----------
        df_1min : pd.DataFrame
            1-minute DataFrame with a 'Datetime' column or DatetimeIndex.
        timeframe : str
            Pandas offset alias, e.g. ``"5min"`` or ``"60min"``.

        Returns
        -------
        pd.DataFrame
            Resampled OHLCV data with 'Datetime' as a regular column.
        """
        df = df_1min.copy()

        if "Datetime" in df.columns:
            df = df.set_index("Datetime")

        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        agg_rules = {
            "Open":   "first",
            "High":   "max",
            "Low":    "min",
            "Close":  "last",
            "Volume": "sum",
        }

        df_resampled = (
            df.resample(timeframe, label="left", closed="left")
            .agg(agg_rules)
            .dropna()
            .reset_index()
            .rename(columns={"index": "Datetime"})
        )

        # Ensure the time column is always named 'Datetime'
        if df_resampled.columns[0] != "Datetime":
            df_resampled = df_resampled.rename(
                columns={df_resampled.columns[0]: "Datetime"}
            )

        logger.info(
            "Resampled to %s: %d bars (from %d 1-min bars)",
            timeframe, len(df_resampled), len(df_1min),
        )
        return df_resampled

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def fetch_5k(self, periods_1min: int = 20_000) -> pd.DataFrame:
        """Fetch and return 5-minute K-line data."""
        return self.resample_to_timeframe(self.fetch_1min_bars(periods_1min), "5min")

    def fetch_60k(self, periods_1min: int = 20_000) -> pd.DataFrame:
        """Fetch and return 60-minute K-line data."""
        return self.resample_to_timeframe(self.fetch_1min_bars(periods_1min), "60min")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise(kbars) -> pd.DataFrame:
        """
        Convert a Shioaji Kbars object to a clean OHLCV DataFrame.

        Steps
        -----
        1. pd.DataFrame({**kbars})    — unpack Shioaji's named-tuple-like object
        2. Normalise column names     — lowercase then rename to Title-Case
        3. ts → DatetimeIndex         — nanosecond Unix epoch → Asia/Taipei
        4. Filter to session windows  — keep day (08:45–13:45) + night (15:00–05:00)
        5. Drop zero-volume bars      — remove Shioaji placeholder candles
        6. Promote index → 'Datetime' column  — consistent with resample output
        """
        df = pd.DataFrame({**kbars})

        if df.empty:
            return df

        # ── Step 2: normalise column names ────────────────────────────────
        df.columns = [c.lower() for c in df.columns]

        rename_map = {
            "open":   "Open",
            "high":   "High",
            "low":    "Low",
            "close":  "Close",
            "volume": "Volume",
        }
        df = df.rename(columns=rename_map)

        required = ["Open", "High", "Low", "Close", "Volume"]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Shioaji kbars response is missing columns: {missing}")

        # ── Step 3: ts → tz-aware Asia/Taipei DatetimeIndex ──────────────
        # Shioaji stores ts as nanoseconds where the epoch base is CST
        # (UTC+8) wall-clock time, NOT UTC.  Treat as naive then localize.
        ts_col = df["ts"]
        if pd.api.types.is_integer_dtype(ts_col):
            dt_index = pd.to_datetime(ts_col, unit="ns").dt.tz_localize("Asia/Taipei")
        else:
            dt_index = pd.to_datetime(ts_col).dt.tz_localize("Asia/Taipei")

        df["Datetime"] = dt_index
        df = df.set_index("Datetime")[required].copy()

        # ── Step 4: filter to TXF session windows ────────────────────────
        bar_times = df.index.time
        session_mask = pd.Series(bar_times, index=df.index).apply(_is_in_session)
        df = df[session_mask.values]

        # ── Step 5: drop zero-volume placeholder bars ─────────────────────
        df = df[df["Volume"] > 0]

        # ── Step 6: sort and promote index ────────────────────────────────
        df = df.dropna(subset=["Close"]).sort_index()
        df.index.name = "Datetime"
        df = df.reset_index()

        return df

    @staticmethod
    def _check_continuity(df: pd.DataFrame) -> None:
        """
        Log a warning if unexpected intra-session gaps are detected.

        Gaps spanning a legitimate session break (05:01–08:44 and 13:46–14:59)
        are expected and ignored.  Gaps > 5 minutes inside a session are flagged.
        """
        if "Datetime" not in df.columns or len(df) < 2:
            return

        dt = pd.to_datetime(df["Datetime"])
        diffs = dt.diff().dropna()
        # Flag gaps > 5 minutes
        large_gaps = diffs[diffs > pd.Timedelta(minutes=5)]

        suspicious = []
        for ts, gap in large_gaps.items():
            gap_time = dt.loc[ts].time()
            # Skip gaps that straddle the known closed windows
            if dtime(5, 0) < gap_time <= dtime(8, 45):
                continue  # morning gap (close → day open)
            if dtime(13, 45) < gap_time <= dtime(15, 0):
                continue  # lunch / pre-night gap
            suspicious.append((dt.loc[ts], gap))

        if suspicious:
            logger.warning(
                "⚠️  偵測到 %d 個非預期的行情缺口（超過 5 分鐘）。"
                " 可能為節假日、API 斷線或夜盤資料缺失。",
                len(suspicious),
            )
            for ts, gap in suspicious[:5]:  # log at most 5 examples
                logger.warning("   缺口位置: %s  缺口大小: %s", ts, gap)


# ===========================================================================
# Backwards-compatible alias
# ===========================================================================

YFinanceDataFetcher = ShioajiDataFetcher


# ===========================================================================
# Standalone convenience function
# ===========================================================================

def fetch_data(
    symbol:    str = ShioajiDataFetcher._CONTRACT_CODE,
    timeframe: str = "1min",
    periods:   int = 20_000,
    start:     "str | None" = None,
    end:       "str | None" = None,
) -> pd.DataFrame:
    """
    Top-level convenience function for fetching TXF00 K-line data.

    Parameters
    ----------
    symbol    : str   Ignored (always fetches TXF00); kept for API compatibility.
    timeframe : str   "1min", "5min", "15min", "30min", or "60min".
    periods   : int   Soft cap on 1-min bars before resampling.
    start     : str   Date string "yyyy-mm-dd" (optional).
    end       : str   Date string "yyyy-mm-dd" (optional).

    Returns
    -------
    pd.DataFrame
        OHLCV DataFrame at the requested timeframe.
    """
    fetcher = ShioajiDataFetcher(symbol=symbol)
    df_1min = fetcher.fetch_1min_bars(periods=periods, start=start, end=end)

    if timeframe == "1min":
        return df_1min
    return ShioajiDataFetcher.resample_to_timeframe(df_1min, timeframe)
