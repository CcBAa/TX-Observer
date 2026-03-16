"""
fetcher.py — Data fetching and resampling for TX-Observer.

Fetches TX Futures (台指期) 1-minute OHLCV bars from Yahoo Finance
using the symbol WTX=F (near-month contract).

Limitations:
  yfinance 1m interval supports a maximum 7-day lookback window.
  Available bar count depends on how many sessions fall within that window.
  MA lines requiring more bars (e.g. MA240 on 5K) will appear gradually.

Retry:
  A simple fixed-delay retry loop guards against transient yfinance failures.
"""

import logging
import time
from datetime import date, datetime, timedelta

import pandas as pd
import pytz
import yfinance as yf

logger = logging.getLogger("tx_observer.fetcher")

TW_TZ   = pytz.timezone("Asia/Taipei")
_SYMBOL = "WTX=F"

_MAX_RETRIES: int = 3
_RETRY_DELAY: int = 5   # seconds between retries


# ===========================================================================
# Public class
# ===========================================================================

class YFinanceDataFetcher:
    """
    Fetches TX Futures 1-minute OHLCV bars from Yahoo Finance (WTX=F).

    Usage
    -----
    fetcher = YFinanceDataFetcher()
    df_1min = fetcher.fetch_1min_bars(periods=1200)
    df_5k   = YFinanceDataFetcher.resample_to_timeframe(df_1min, "5min")
    df_60k  = YFinanceDataFetcher.resample_to_timeframe(df_1min, "60min")
    """

    def __init__(self) -> None:
        self._latest_close: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_1min_bars(self, periods: int = 1200) -> pd.DataFrame:
        """
        Fetch the latest *periods* 1-minute bars for WTX=F.

        Downloads the last 5 calendar days of 1-minute data (the maximum
        yfinance allows for 1m interval is 7 days) and returns the most
        recent *periods* bars.

        Parameters
        ----------
        periods : int
            Maximum number of bars to return (most recent).

        Returns
        -------
        pd.DataFrame
            Columns: Datetime (tz-aware UTC+8), Open, High, Low, Close, Volume

        Raises
        ------
        RuntimeError
            If all retry attempts fail or the response is empty.
        """
        raw = self._download_with_retry(interval="1m", period="5d")
        df  = self._normalise(raw)

        if len(df) > periods:
            df = df.iloc[-periods:].reset_index(drop=True)

        self._latest_close = float(df["Close"].iloc[-1])
        logger.info(
            "YFinanceDataFetcher: %d 1-min bars fetched for %s | latest close = %.0f",
            len(df), _SYMBOL, self._latest_close,
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

    def _download_with_retry(self, interval: str, period: str) -> pd.DataFrame:
        """
        Call yf.download() with up to _MAX_RETRIES attempts.

        Raises RuntimeError if every attempt fails or returns empty data.
        """
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                df = yf.download(
                    _SYMBOL,
                    interval=interval,
                    period=period,
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                )
                if df.empty:
                    raise ValueError(
                        f"yfinance returned an empty DataFrame for {_SYMBOL} "
                        f"(interval={interval}, period={period}). "
                        "Market may be closed or symbol unavailable."
                    )
                return df

            except Exception as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "yfinance download attempt %d/%d failed: %s — retrying in %ds...",
                        attempt, _MAX_RETRIES, exc, _RETRY_DELAY,
                    )
                    time.sleep(_RETRY_DELAY)

        raise RuntimeError(
            f"yfinance download failed after {_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    @staticmethod
    def _normalise(df: pd.DataFrame) -> pd.DataFrame:
        """
        Normalise a raw yfinance DataFrame into the standard OHLCV format.

        Handles:
        - Multi-level columns produced by newer yfinance versions
        - Timezone conversion to Asia/Taipei
        - Column renaming to Title-Case (Open/High/Low/Close/Volume)
        - Reset index so 'Datetime' becomes a regular column
        """
        # ── Multi-level columns (yfinance >= 0.2 with single ticker) ──────
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # ── Normalise column names to Title-Case ──────────────────────────
        df.columns = [c.title() for c in df.columns]

        required = ["Open", "High", "Low", "Close", "Volume"]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"yfinance response missing columns: {missing}")

        df = df[required].copy()

        # ── Timezone → Asia/Taipei ────────────────────────────────────────
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(TW_TZ)

        # ── Drop NaN rows (e.g. outside-session padding) ─────────────────
        df = df.dropna(subset=["Close"])
        df = df.sort_index()

        # ── Promote index to regular 'Datetime' column ────────────────────
        df.index.name = "Datetime"
        df = df.reset_index()

        return df


# ===========================================================================
# Near-month symbol helpers (kept for reference / future use)
# ===========================================================================

def _third_wednesday(year: int, month: int) -> date:
    """Return the date of the third Wednesday of the given month."""
    d = date(year, month, 1)
    days_to_first_wed = (2 - d.weekday()) % 7
    return d + timedelta(days=days_to_first_wed + 14)
