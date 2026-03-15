"""
fetcher.py — Data fetching and resampling for TX-Observer.

Currently ships a MockDataFetcher that simulates realistic TX Futures
(台指期近月) 1-minute OHLCV bars using geometric Brownian motion.
Replace MockDataFetcher with a real broker API implementation when ready;
the resample_to_timeframe() interface remains the same.
"""

import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz

logger = logging.getLogger("tx_observer.fetcher")

TW_TZ = pytz.timezone("Asia/Taipei")

# ---------------------------------------------------------------------------
# Constants — TX Futures characteristics
# ---------------------------------------------------------------------------
_BASE_PRICE: float = 20_000.0          # Approximate index level
_DAILY_VOLATILITY: float = 0.015       # ~1.5 % daily standard deviation
_TICK_SIZE: float = 1.0                # Minimum price move = 1 point
_TRADING_MINUTES_PER_DAY: int = 840    # Combined day + night session (~14 h)
_MIN_VOL: int = 300                    # Minimum contracts per 1-min bar
_MAX_VOL: int = 3_000                  # Maximum contracts per 1-min bar


class MockDataFetcher:
    """
    Simulates TX Futures 1-minute OHLCV bars using geometric Brownian motion
    with intraday volume weighting and open/close surge effects.

    Usage
    -----
    fetcher = MockDataFetcher()
    df_1min = fetcher.fetch_1min_bars(periods=480)
    df_5k   = MockDataFetcher.resample_to_timeframe(df_1min, "5min")
    df_60k  = MockDataFetcher.resample_to_timeframe(df_1min, "60min")
    """

    def __init__(self, base_price: float = _BASE_PRICE) -> None:
        self.base_price = base_price
        self._latest_close: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_1min_bars(self, periods: int = 480) -> pd.DataFrame:
        """
        Generate *periods* 1-minute OHLCV bars ending at the current minute.

        Parameters
        ----------
        periods:
            Number of 1-minute bars to generate (default 480 = 8 hours).

        Returns
        -------
        pd.DataFrame
            Columns: Datetime (tz-aware UTC+8), Open, High, Low, Close, Volume
        """
        if periods < 2:
            raise ValueError("periods must be >= 2")

        now_tw = datetime.now(tz=TW_TZ).replace(second=0, microsecond=0)
        timestamps = [now_tw - timedelta(minutes=i) for i in range(periods - 1, -1, -1)]

        # Per-minute volatility derived from daily vol
        min_vol = _DAILY_VOLATILITY / np.sqrt(_TRADING_MINUTES_PER_DAY)

        # Random drift (simulates intraday trending / mean-reversion regime)
        drift = np.random.choice([-1.0, 0.0, 1.0]) * min_vol * 0.25

        # Log-returns with slight autocorrelation (momentum)
        raw_returns = np.random.normal(drift, min_vol, periods)
        returns = _apply_momentum(raw_returns, alpha=0.15)

        # Build close price series
        closes = np.empty(periods)
        closes[0] = self.base_price * np.exp(np.random.normal(0, min_vol * 2))
        for i in range(1, periods):
            raw = closes[i - 1] * np.exp(returns[i])
            closes[i] = round(raw / _TICK_SIZE) * _TICK_SIZE

        # Intra-bar high/low spread (proportional to bar vol)
        spread_vol = min_vol * 0.6
        half_spread = np.abs(np.random.normal(0, spread_vol, periods)) * closes

        highs = np.round((closes + half_spread) / _TICK_SIZE) * _TICK_SIZE
        lows  = np.round((closes - half_spread) / _TICK_SIZE) * _TICK_SIZE

        # Opens = previous close with small gap
        opens = np.empty(periods)
        opens[0] = closes[0] * np.exp(np.random.normal(0, spread_vol))
        opens[0] = round(opens[0] / _TICK_SIZE) * _TICK_SIZE
        opens[1:] = np.roll(closes, 1)[1:]

        # Ensure OHLC consistency: High >= max(O, C), Low <= min(O, C)
        for i in range(periods):
            highs[i] = round(max(highs[i], opens[i], closes[i]) / _TICK_SIZE) * _TICK_SIZE
            lows[i]  = round(min(lows[i],  opens[i], closes[i]) / _TICK_SIZE) * _TICK_SIZE

        # Volume: U-shaped curve (surge at open and close)
        volumes = _generate_volume_curve(periods, _MIN_VOL, _MAX_VOL)

        df = pd.DataFrame(
            {
                "Datetime": timestamps,
                "Open":     opens.astype(float),
                "High":     highs.astype(float),
                "Low":      lows.astype(float),
                "Close":    closes.astype(float),
                "Volume":   volumes,
            }
        )

        self._latest_close = float(closes[-1])
        logger.info(
            "MockDataFetcher: generated %d 1-min bars | latest close = %.0f",
            periods,
            self._latest_close,
        )
        return df

    def get_latest_price(self) -> float:
        """Return the most recent simulated close price."""
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
        df_1min:
            1-minute DataFrame with a 'Datetime' column or DatetimeIndex.
        timeframe:
            Pandas offset alias, e.g. ``"5min"`` (5-minute) or
            ``"60min"`` / ``"1h"`` (60-minute).

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

        # Ensure the Datetime column is named correctly after reset_index
        if df_resampled.columns[0] != "Datetime":
            df_resampled = df_resampled.rename(columns={df_resampled.columns[0]: "Datetime"})

        logger.info(
            "Resampled to %s: %d bars (from %d 1-min bars)",
            timeframe,
            len(df_resampled),
            len(df_1min),
        )
        return df_resampled

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def fetch_5k(self, periods_1min: int = 300) -> pd.DataFrame:
        """Fetch and return 5-minute K-line data."""
        df = self.fetch_1min_bars(periods=periods_1min)
        return self.resample_to_timeframe(df, "5min")

    def fetch_60k(self, periods_1min: int = 1_440) -> pd.DataFrame:
        """Fetch and return 60-minute K-line data."""
        df = self.fetch_1min_bars(periods=periods_1min)
        return self.resample_to_timeframe(df, "60min")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _apply_momentum(returns: np.ndarray, alpha: float = 0.15) -> np.ndarray:
    """Apply simple AR(1)-style momentum smoothing to a return series."""
    smoothed = np.empty_like(returns)
    smoothed[0] = returns[0]
    for i in range(1, len(returns)):
        smoothed[i] = alpha * smoothed[i - 1] + (1 - alpha) * returns[i]
    return smoothed


def _generate_volume_curve(periods: int, vol_min: int, vol_max: int) -> np.ndarray:
    """
    Generate a U-shaped intraday volume curve.
    The first and last 10 % of bars receive ~2× average volume.
    """
    base = np.random.randint(vol_min, vol_max, periods).astype(float)
    surge = max(1, int(periods * 0.10))

    # Opening surge
    open_mult = np.linspace(2.0, 1.0, surge)
    base[:surge] *= open_mult

    # Closing surge
    close_mult = np.linspace(1.0, 2.0, surge)
    base[-surge:] *= close_mult

    return base.astype(int)
