"""
fetcher.py — Data fetching and resampling for TX-Observer.

MockDataFetcher simulates realistic TX Futures (台指期近月) 1-minute OHLCV
bars using a Markov regime-switching model with GARCH(1,1)-like volatility
clustering, producing charts that resemble real market structure:

  • Three regimes — uptrend / downtrend / sideways — driven by a Markov chain
    with high persistence (~100-bar average duration), so trends and ranges
    are visually obvious across a session.
  • GARCH(1,1)-style volatility clustering: elevated vol persists after large
    moves, creating the bursts and quiet patches seen in real futures charts.
  • Round-number gravity: mild mean-reversion toward every 100-pt level
    (e.g. 20 000, 20 100) simulates psychological support/resistance.
  • Realistic intrabar high/low: wick size scales with volatility; trending
    bars have smaller counter-trend wicks than reversal/doji-like bars.
  • Volume correlated with price-move magnitude and intraday U-curve
    (surge at open and close of each session).

Replace MockDataFetcher with a real Shioaji (永豐金) broker API when ready;
the public interface — fetch_1min_bars() and resample_to_timeframe() —
remains unchanged.
"""

import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz

logger = logging.getLogger("tx_observer.fetcher")

TW_TZ = pytz.timezone("Asia/Taipei")

# ---------------------------------------------------------------------------
# Market constants
# ---------------------------------------------------------------------------
_BASE_PRICE: float = 20_000.0
_TICK_SIZE:  float = 1.0
_MIN_VOL:    int   = 300
_MAX_VOL:    int   = 3_000

# Per-minute volatility derived from ~1.5 % daily vol over 840 trading minutes
_DAILY_VOL:    float = 0.015
_TRADING_MINS: int   = 840
_BASE_MIN_VOL: float = _DAILY_VOL / np.sqrt(_TRADING_MINS)   # ≈ 0.000518

# ---------------------------------------------------------------------------
# Markov regime-switching
# ---------------------------------------------------------------------------
# States:  0 = uptrend,  1 = downtrend,  2 = sideways
_UPTREND, _DOWNTREND, _SIDEWAYS = 0, 1, 2

# High persistence keeps each regime visible for ~100 bars on average
# (1 / (1 - diagonal element))
_TRANSITION = np.array([
    [0.990, 0.003, 0.007],   # uptrend
    [0.003, 0.990, 0.007],   # downtrend
    [0.005, 0.005, 0.990],   # sideways
])

# Per-minute drift (fractional log-return) for each regime
_REGIME_DRIFT = np.array([+0.00010, -0.00010, 0.0])

# Volatility multiplier applied on top of the GARCH baseline
_REGIME_VOL_MULT = np.array([1.4, 1.4, 0.55])

# ---------------------------------------------------------------------------
# GARCH(1,1) parameters  (omega + alpha + beta < 1 → stationary)
# ---------------------------------------------------------------------------
_GARCH_OMEGA: float = _BASE_MIN_VOL ** 2 * 0.05   # baseline variance
_GARCH_ALPHA: float = 0.10                          # shock impact
_GARCH_BETA:  float = 0.85                          # variance persistence

# ---------------------------------------------------------------------------
# Round-number gravity
# ---------------------------------------------------------------------------
_ROUND_LEVEL: float = 100.0    # key psychological levels every 100 pts
_GRAVITY:     float = 0.04     # pull toward nearest level per bar (fractional)


# ===========================================================================
# Public class
# ===========================================================================

class MockDataFetcher:
    """
    Simulates TX Futures 1-minute OHLCV bars with realistic market structure.

    Usage
    -----
    fetcher = MockDataFetcher()
    df_1min = fetcher.fetch_1min_bars(periods=1200)
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
        periods : int
            Number of 1-minute bars (default 480 = 8 hours).

        Returns
        -------
        pd.DataFrame
            Columns: Datetime (tz-aware UTC+8), Open, High, Low, Close, Volume
        """
        if periods < 2:
            raise ValueError("periods must be >= 2")

        now_tw = datetime.now(tz=TW_TZ).replace(second=0, microsecond=0)
        timestamps = [now_tw - timedelta(minutes=i) for i in range(periods - 1, -1, -1)]

        # ── Step 1: Markov regime sequence ────────────────────────────────
        regimes = _generate_regimes(periods)

        # ── Step 2: GARCH(1,1) volatility series ─────────────────────────
        vol_series = _generate_garch_vol(periods, _REGIME_VOL_MULT[regimes])

        # ── Step 3: Close price series ────────────────────────────────────
        closes = _generate_closes(self.base_price, periods, regimes, vol_series)

        # ── Step 4: Intrabar OHLC ─────────────────────────────────────────
        opens, highs, lows = _build_ohlc(closes, vol_series, regimes)

        # ── Step 5: Volume ────────────────────────────────────────────────
        log_returns = np.abs(np.diff(np.log(np.maximum(closes, 1e-6))))
        volumes = _generate_volume(periods, log_returns, regimes)

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
            timeframe,
            len(df_resampled),
            len(df_1min),
        )
        return df_resampled

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def fetch_5k(self, periods_1min: int = 1200) -> pd.DataFrame:
        """Fetch and return 5-minute K-line data."""
        df = self.fetch_1min_bars(periods=periods_1min)
        return self.resample_to_timeframe(df, "5min")

    def fetch_60k(self, periods_1min: int = 1200) -> pd.DataFrame:
        """Fetch and return 60-minute K-line data."""
        df = self.fetch_1min_bars(periods=periods_1min)
        return self.resample_to_timeframe(df, "60min")


# ===========================================================================
# Internal helpers
# ===========================================================================

def _generate_regimes(periods: int) -> np.ndarray:
    """
    Sample a Markov chain regime sequence of length *periods*.

    Returns an int array with values 0 (uptrend), 1 (downtrend), 2 (sideways).
    Initial state is drawn from the stationary distribution.
    """
    # Stationary distribution via eigen-decomposition fallback
    init_probs = np.array([0.30, 0.30, 0.40])
    regimes = np.empty(periods, dtype=int)
    regimes[0] = np.random.choice(_TRANSITION.shape[0], p=init_probs)
    for i in range(1, periods):
        regimes[i] = np.random.choice(
            _TRANSITION.shape[0], p=_TRANSITION[regimes[i - 1]]
        )
    return regimes


def _generate_garch_vol(periods: int, regime_vol_mult: np.ndarray) -> np.ndarray:
    """
    Generate a GARCH(1,1)-style conditional volatility series.

    The unconditional variance is anchored to *_BASE_MIN_VOL*; regime
    multipliers scale it up (trending) or down (sideways).

    Returns
    -------
    np.ndarray
        Per-bar standard deviation (same unit as log-returns).
    """
    vol = np.empty(periods)
    h = _BASE_MIN_VOL ** 2      # initial conditional variance
    eps = 0.0                   # previous shock

    for i in range(periods):
        # Regime-scaled unconditional variance acts as a time-varying omega
        target_var = (_BASE_MIN_VOL * regime_vol_mult[i]) ** 2
        omega_i    = target_var * (1 - _GARCH_ALPHA - _GARCH_BETA)
        h = omega_i + _GARCH_ALPHA * eps ** 2 + _GARCH_BETA * h
        h = max(h, 1e-12)       # numerical floor
        sigma = np.sqrt(h)
        vol[i] = sigma
        eps = np.random.normal(0, sigma)

    return vol


def _generate_closes(
    base_price: float,
    periods: int,
    regimes: np.ndarray,
    vol_series: np.ndarray,
) -> np.ndarray:
    """
    Build a close-price series using regime drift, GARCH vol, and
    round-number gravity.
    """
    closes = np.empty(periods)
    # Randomise starting price slightly around the base
    closes[0] = round(
        base_price * np.exp(np.random.normal(0, _BASE_MIN_VOL * 3)) / _TICK_SIZE
    ) * _TICK_SIZE

    for i in range(1, periods):
        drift  = _REGIME_DRIFT[regimes[i]]
        shock  = np.random.normal(0, vol_series[i])

        # Round-number gravity: fractional pull toward nearest _ROUND_LEVEL
        nearest = round(closes[i - 1] / _ROUND_LEVEL) * _ROUND_LEVEL
        gravity = _GRAVITY * (nearest - closes[i - 1]) / closes[i - 1]

        log_ret = drift + shock + gravity
        raw     = closes[i - 1] * np.exp(log_ret)
        closes[i] = round(raw / _TICK_SIZE) * _TICK_SIZE

    return closes


def _build_ohlc(
    closes: np.ndarray,
    vol_series: np.ndarray,
    regimes: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Derive Open, High, Low arrays from the Close series.

    Wick behaviour:
    - Trending bars (uptrend/downtrend): smaller counter-trend wick,
      larger wick in the direction of the move.
    - Sideways bars: symmetric wicks (doji/spinning-top shape).
    """
    periods = len(closes)
    opens   = np.empty(periods)
    highs   = np.empty(periods)
    lows    = np.empty(periods)

    # Open = previous close (no gap for intraday; slight gap at bar 0)
    opens[0] = round(
        closes[0] * np.exp(np.random.normal(0, vol_series[0] * 0.5)) / _TICK_SIZE
    ) * _TICK_SIZE
    opens[1:] = closes[:-1]

    for i in range(periods):
        body_top    = max(opens[i], closes[i])
        body_bottom = min(opens[i], closes[i])
        sigma       = vol_series[i] * closes[i]   # in price points

        if regimes[i] == _UPTREND:
            upper_wick = abs(np.random.normal(0, sigma * 0.8))
            lower_wick = abs(np.random.normal(0, sigma * 0.3))
        elif regimes[i] == _DOWNTREND:
            upper_wick = abs(np.random.normal(0, sigma * 0.3))
            lower_wick = abs(np.random.normal(0, sigma * 0.8))
        else:  # sideways — symmetric wicks
            upper_wick = abs(np.random.normal(0, sigma * 0.6))
            lower_wick = abs(np.random.normal(0, sigma * 0.6))

        highs[i] = round((body_top    + upper_wick) / _TICK_SIZE) * _TICK_SIZE
        lows[i]  = round((body_bottom - lower_wick) / _TICK_SIZE) * _TICK_SIZE

        # Guarantee OHLC consistency
        highs[i] = max(highs[i], body_top)
        lows[i]  = min(lows[i],  body_bottom)

    return opens, highs, lows


def _generate_volume(
    periods: int,
    log_returns: np.ndarray,
    regimes: np.ndarray,
) -> np.ndarray:
    """
    Generate volume correlated with price-move magnitude and intraday curve.

    Larger absolute returns → higher volume (confirms moves).
    Trending regimes → slightly higher average volume than sideways.
    """
    # Base random volume
    base = np.random.randint(_MIN_VOL, _MAX_VOL, periods).astype(float)

    # Scale by absolute return magnitude (normalised) — first bar has no return
    if len(log_returns) > 0:
        max_ret = log_returns.max() if log_returns.max() > 0 else 1.0
        ret_scale = np.ones(periods)
        ret_scale[1:] = 1.0 + 2.5 * (log_returns / max_ret)
        base *= ret_scale

    # Regime volume boost: trends draw more participation
    regime_boost = np.where(regimes == _SIDEWAYS, 0.7, 1.2)
    base *= regime_boost

    # Intraday U-curve (surge at open and close)
    surge_bars = max(1, int(periods * 0.10))
    base[:surge_bars]  *= np.linspace(2.2, 1.0, surge_bars)
    base[-surge_bars:] *= np.linspace(1.0, 2.2, surge_bars)

    return np.clip(base, _MIN_VOL, _MAX_VOL * 3).astype(int)
