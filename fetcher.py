"""
fetcher.py — Data fetching and resampling for TX-Observer.

Uses Shioaji (永豐金 API) to fetch 2330 (TSMC) spot stock 1-minute
OHLCV bars while TX Futures permissions are pending.

Public interface is kept identical to the original YFinanceDataFetcher so
main.py needs zero changes (YFinanceDataFetcher is aliased to ShioajiDataFetcher).

A module-level Shioaji API singleton is created on first use and logged out
automatically via atexit when the process exits.
"""

import atexit
import logging
import os
from datetime import date, timedelta

from pathlib import Path

import pandas as pd
import pytz
import shioaji as sj
from dotenv import load_dotenv

# Use the same .env path resolution as config.py (absolute, not CWD-relative)
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logger = logging.getLogger("tx_observer.fetcher")

TW_TZ     = pytz.timezone("Asia/Taipei")
_SYMBOL   = "2330"
_EXCHANGE = "TSE"


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

    # Ensure logout runs even on unhandled exceptions / KeyboardInterrupt
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
    Fetches 2330 (TSMC) 1-minute OHLCV bars from Shioaji.

    Usage
    -----
    fetcher = ShioajiDataFetcher()
    df_1min = fetcher.fetch_1min_bars(periods=1200)
    df_5k   = ShioajiDataFetcher.resample_to_timeframe(df_1min, "5min")
    df_60k  = ShioajiDataFetcher.resample_to_timeframe(df_1min, "60min")

    To switch back to TX Futures (WTX=F via yfinance), replace the alias at
    the bottom of this file:
        YFinanceDataFetcher = <original YFinanceDataFetcher class>
    """

    def __init__(self, symbol: str = _SYMBOL) -> None:
        self._symbol       = symbol
        self._latest_close: "float | None" = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_1min_bars(
        self,
        periods: int = 1200,
        start:   "str | None" = None,
        end:     "str | None" = None,
    ) -> pd.DataFrame:
        """
        Fetch the latest *periods* 1-minute bars for the configured stock.

        Parameters
        ----------
        periods : int
            Maximum number of bars to return (most recent).
            Ignored when both *start* and *end* are supplied explicitly.
        start : str, optional
            Date string "yyyy-mm-dd". Defaults to 10 calendar days ago.
        end : str, optional
            Date string "yyyy-mm-dd". Defaults to today.

        Returns
        -------
        pd.DataFrame
            Columns: Datetime (tz-aware Asia/Taipei), Open, High, Low, Close, Volume

        Raises
        ------
        RuntimeError
            If Shioaji login or kbars call fails.
        ValueError
            If the response contains no rows.
        """
        today = date.today()
        if end is None:
            end = today.strftime("%Y-%m-%d")
        if start is None:
            # 10 calendar days back ensures we capture enough trading sessions
            start = (today - timedelta(days=10)).strftime("%Y-%m-%d")

        api      = _get_api()
        contract = api.Contracts.Stocks[_EXCHANGE][self._symbol]

        logger.info(
            "Fetching 1-min kbars: %s  %s → %s", self._symbol, start, end
        )

        try:
            kbars = api.kbars(contract, start=start, end=end)
        except Exception as exc:
            logger.error("api.kbars() raised an exception: %s", exc)
            raise RuntimeError(f"Shioaji kbars fetch failed: {exc}") from exc

        df = self._normalise(kbars)

        if df.empty:
            logger.error(
                "api.kbars() returned no data for %s (%s → %s). "
                "Market may be closed or the symbol is unavailable.",
                self._symbol, start, end,
            )
            raise ValueError(
                f"No kbar data returned for {self._symbol} ({start} → {end})"
            )

        if len(df) > periods:
            df = df.iloc[-periods:].reset_index(drop=True)

        self._latest_close = float(df["Close"].iloc[-1])
        logger.info(
            "ShioajiDataFetcher: %d 1-min bars | %s | latest close = %.2f",
            len(df), self._symbol, self._latest_close,
        )
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

    def fetch_5k(self, periods_1min: int = 1200) -> pd.DataFrame:
        """Fetch and return 5-minute K-line data."""
        return self.resample_to_timeframe(self.fetch_1min_bars(periods_1min), "5min")

    def fetch_60k(self, periods_1min: int = 1200) -> pd.DataFrame:
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
        4. Drop NaN rows, sort asc    — remove any padding outside session
        5. Promote index → 'Datetime' column  — consistent with resample output
        """
        df = pd.DataFrame({**kbars})

        if df.empty:
            return df

        # ── Step 2: normalise all column names to lowercase first ─────────
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
        # Shioaji returns ts as integer nanoseconds since Unix epoch.
        # Guard against the case where it is already a datetime.
        ts_col = df["ts"]
        if pd.api.types.is_integer_dtype(ts_col):
            dt_index = pd.to_datetime(ts_col, unit="ns", utc=True).dt.tz_convert(
                "Asia/Taipei"
            )
        else:
            dt_index = pd.to_datetime(ts_col, utc=True).dt.tz_convert("Asia/Taipei")

        df["Datetime"] = dt_index
        df = df.set_index("Datetime")[required].copy()

        # ── Steps 4 & 5 ───────────────────────────────────────────────────
        df = df.dropna(subset=["Close"]).sort_index()
        df.index.name = "Datetime"
        df = df.reset_index()

        return df


# ===========================================================================
# Backwards-compatible alias
# Lets main.py keep `from fetcher import YFinanceDataFetcher` unchanged.
# ===========================================================================

YFinanceDataFetcher = ShioajiDataFetcher


# ===========================================================================
# Standalone convenience function
# ===========================================================================

def fetch_data(
    symbol:    str = _SYMBOL,
    timeframe: str = "1min",
    periods:   int = 1200,
    start:     "str | None" = None,
    end:       "str | None" = None,
) -> pd.DataFrame:
    """
    Top-level convenience function — easy to call and easy to swap back
    to a different data source.

    Parameters
    ----------
    symbol    : str   Stock/futures symbol, e.g. "2330".
    timeframe : str   "1min", "5min", "15min", "30min", or "60min".
    periods   : int   Max number of 1-min bars to fetch before resampling.
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
