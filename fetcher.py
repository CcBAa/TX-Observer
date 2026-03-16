"""
fetcher.py — Data fetching and resampling for TX-Observer.

Fetches real TX Futures (台指期近月) 1-minute OHLCV bars from the
Fugle Market Data API (富果行情 API) via fugle-marketdata SDK.

Symbol resolution:
  TX near-month symbol is determined automatically (e.g. TXFC6 for Mar 2026).
  Rolls to the next month after the 3rd Wednesday (settlement day).

Data strategy:
  Uses the historical candles endpoint with a 7-day lookback window so that
  the result spans multiple sessions — enough bars for MA240 on the 5K chart.
"""

import logging
from datetime import date, datetime, timedelta

import pandas as pd
import pytz
from fugle_marketdata import RestClient

logger = logging.getLogger("tx_observer.fetcher")

TW_TZ = pytz.timezone("Asia/Taipei")


# ===========================================================================
# Public class
# ===========================================================================

class FugleDataFetcher:
    """
    Fetches TX Futures 1-minute OHLCV bars from the Fugle Market Data API.

    Usage
    -----
    fetcher = FugleDataFetcher(api_key="YOUR_TOKEN")
    df_1min = fetcher.fetch_1min_bars(periods=1200)
    df_5k   = FugleDataFetcher.resample_to_timeframe(df_1min, "5min")
    df_60k  = FugleDataFetcher.resample_to_timeframe(df_1min, "60min")
    """

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("FUGLE_API_TOKEN is empty.")
        self._client = RestClient(api_key=api_key)
        self._latest_close: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_1min_bars(self, periods: int = 1200) -> pd.DataFrame:
        """
        Fetch the latest *periods* 1-minute bars for the TX near-month contract.

        Looks back up to 7 calendar days to ensure enough bars across sessions.

        Parameters
        ----------
        periods : int
            Maximum number of 1-minute bars to return (most recent).

        Returns
        -------
        pd.DataFrame
            Columns: Datetime (tz-aware UTC+8), Open, High, Low, Close, Volume

        Raises
        ------
        RuntimeError
            If the API returns no data or an unexpected response.
        """
        now     = datetime.now(tz=TW_TZ)
        symbol  = _get_near_month_symbol(now)
        date_from = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        date_to   = now.strftime("%Y-%m-%d")

        logger.info(
            "Fetching 1-min bars: symbol=%s  from=%s  to=%s",
            symbol, date_from, date_to,
        )

        try:
            response = self._client.futopt.historical.candles(**{
                "symbol":    symbol,
                "from":      date_from,
                "to":        date_to,
                "timeframe": "1",
            })
        except Exception as exc:
            raise RuntimeError(f"Fugle API request failed: {exc}") from exc

        candles = response.get("candles") or response.get("data") or []
        if not candles:
            raise RuntimeError(
                f"Fugle API returned no candle data for {symbol} "
                f"({date_from} → {date_to}). Market may be closed."
            )

        df = _parse_candles(candles)

        if len(df) == 0:
            raise RuntimeError(f"No parseable bars returned for {symbol}.")

        # Trim to the most recent `periods` bars
        if len(df) > periods:
            df = df.iloc[-periods:].reset_index(drop=True)

        self._latest_close = float(df["Close"].iloc[-1])
        logger.info(
            "FugleDataFetcher: %d 1-min bars fetched for %s | latest close = %.0f",
            len(df), symbol, self._latest_close,
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


# ===========================================================================
# Internal helpers
# ===========================================================================

def _get_near_month_symbol(dt: datetime) -> str:
    """
    Return the near-month TX Futures symbol for a given datetime.

    TX futures expire on the third Wednesday of each month.
    After settlement, the contract rolls to the next month.

    Examples
    --------
    2026-03-16 (before 3rd Wed Mar 18) → TXFC6
    2026-03-20 (after  3rd Wed Mar 18) → TXFD6
    """
    year  = dt.year
    month = dt.month

    expiry = _third_wednesday(year, month)
    if dt.date() >= expiry:
        if month == 12:
            month, year = 1, year + 1
        else:
            month += 1

    month_code = chr(ord("A") + month - 1)   # A=Jan … L=Dec
    year_code  = str(year % 10)
    return f"TXF{month_code}{year_code}"


def _third_wednesday(year: int, month: int) -> date:
    """Return the date of the third Wednesday of the given month."""
    d = date(year, month, 1)
    # Weekday: Mon=0 … Sun=6; Wednesday=2
    days_to_first_wed = (2 - d.weekday()) % 7
    return d + timedelta(days=days_to_first_wed + 14)


def _parse_candles(candles: list[dict]) -> pd.DataFrame:
    """
    Parse a Fugle candles list into a standardised OHLCV DataFrame.

    Handles both tz-aware and tz-naive date strings; normalises all
    timestamps to Asia/Taipei.
    """
    rows = []
    for c in candles:
        try:
            dt = pd.to_datetime(c["date"])
            if dt.tzinfo is None:
                dt = dt.tz_localize(TW_TZ)
            else:
                dt = dt.tz_convert(TW_TZ)
            rows.append({
                "Datetime": dt,
                "Open":     float(c["open"]),
                "High":     float(c["high"]),
                "Low":      float(c["low"]),
                "Close":    float(c["close"]),
                "Volume":   int(c.get("volume", 0)),
            })
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping malformed candle record: %s | error: %s", c, exc)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Datetime").reset_index(drop=True)
    return df
