"""
renderer.py — Headless K-line chart rendering for TX-Observer.

Uses mplfinance with a custom dark theme that follows Taiwan market colour
conventions (red candle = price up, green candle = price down).

The Agg backend is forced so this module works on headless Linux servers
with no display or GUI libraries.
"""

import logging
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # Must be set BEFORE any other matplotlib import

import matplotlib.pyplot as plt  # noqa: E402  (imported after backend override)
import mplfinance as mpf          # noqa: E402
import pandas as pd               # noqa: E402
import pytz                       # noqa: E402

logger = logging.getLogger("tx_observer.renderer")

TW_TZ = pytz.timezone("Asia/Taipei")

# ---------------------------------------------------------------------------
# Style — Taiwan market convention: red = up, green = down
# ---------------------------------------------------------------------------
_MARKET_COLORS = mpf.make_marketcolors(
    up="#EF5350",       # Bullish candle body  (red)
    down="#26A69A",     # Bearish candle body  (green / teal)
    edge="inherit",
    wick="inherit",
    volume={
        "up":   "#EF535088",   # Semi-transparent red for up-volume bars
        "down": "#26A69A88",   # Semi-transparent teal for down-volume bars
    },
)

_DARK_STYLE = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=_MARKET_COLORS,
    mavcolors=["#FFD700", "#00BFFF", "#FF69B4"],   # MA5=gold, MA10=dodger blue, MA20=hot pink
    gridstyle="--",
    gridcolor="#2a2a3e",
    facecolor="#0d1117",        # Axes background
    figcolor="#0d1117",         # Figure background
    y_on_right=True,
    rc={
        "font.size":        9,
        "axes.labelcolor":  "#c9d1d9",
        "axes.edgecolor":   "#30363d",
        "xtick.color":      "#8b949e",
        "ytick.color":      "#8b949e",
        "figure.titlesize": 11,
        "figure.titlecolor": "#e6edf3",
    },
)

# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR = Path("charts")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_chart(
    df: pd.DataFrame,
    timeframe: str,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
) -> Path:
    """
    Render a candlestick chart with MA lines (5, 10, 20) and volume subplot.

    Parameters
    ----------
    df:
        OHLCV DataFrame.  Must contain columns Open, High, Low, Close, Volume
        and either a 'Datetime' column or a DatetimeIndex.
    timeframe:
        Label used in the filename and chart title, e.g. ``"5K"`` or ``"60K"``.
    output_dir:
        Directory where the PNG file will be saved.  Created automatically.

    Returns
    -------
    Path
        Absolute path to the saved PNG file.

    Raises
    ------
    ValueError
        If the DataFrame has fewer than 3 rows (not enough to draw a chart).
    RuntimeError
        If mplfinance raises an unexpected error during rendering.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    now_tw = datetime.now(tz=TW_TZ)
    timestamp_str = now_tw.strftime("%Y%m%d_%H%M")
    filename = f"tx_{timeframe.lower()}_{timestamp_str}.png"
    filepath = (output_dir / filename).resolve()

    df_plot = _prepare_dataframe(df)

    if len(df_plot) < 3:
        raise ValueError(
            f"DataFrame has only {len(df_plot)} row(s); at least 3 are required to render a chart."
        )

    # Warn if not enough bars for MA20
    if len(df_plot) < 20:
        logger.warning(
            "Only %d bars available — MA20 will be incomplete (need >= 20).",
            len(df_plot),
        )

    # ------------------------------------------------------------------
    # Build title
    # ------------------------------------------------------------------
    latest_close = float(df_plot["Close"].iloc[-1])
    prev_close   = float(df_plot["Close"].iloc[-2]) if len(df_plot) > 1 else latest_close
    change       = latest_close - prev_close
    change_pct   = (change / prev_close) * 100.0 if prev_close else 0.0
    change_arrow = "▲" if change >= 0 else "▼"

    title_line1 = (
        f"TX-Observer  |  {timeframe}  |  "
        f"Last: {latest_close:,.0f}  "
        f"{change_arrow} {abs(change):.0f} ({change_pct:+.2f}%)"
    )
    title_line2 = (
        f"Generated: {now_tw.strftime('%Y-%m-%d %H:%M')} (UTC+8)  "
        f"|  MA: 5 (gold)  10 (blue)  20 (pink)"
    )
    title = f"{title_line1}\n{title_line2}"

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------
    try:
        mpf.plot(
            df_plot,
            type="candle",
            style=_DARK_STYLE,
            title=title,
            mav=(5, 10, 20),
            volume=True,
            figsize=(18, 10),
            savefig={"fname": str(filepath), "dpi": 150, "bbox_inches": "tight"},
            warn_too_much_data=2_000,
        )
        plt.close("all")  # Release memory after saving
        logger.info("Chart saved → %s", filepath)
        return filepath

    except Exception as exc:
        plt.close("all")
        logger.error("Failed to render %s chart: %s", timeframe, exc, exc_info=True)
        raise RuntimeError(f"Chart rendering failed for {timeframe}") from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalise the input DataFrame for mplfinance:
      - Ensures a proper DatetimeIndex
      - Converts tz-aware index to Asia/Taipei then strips tz-info
        (mplfinance can handle tz-aware indexes, but stripping avoids
        occasional formatting quirks with some mplfinance versions)
    """
    df = df.copy()

    if "Datetime" in df.columns:
        df = df.set_index("Datetime")

    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    # Normalise timezone: convert to UTC+8, then make tz-naive for plotting
    if df.index.tz is not None:
        df.index = df.index.tz_convert(TW_TZ).tz_localize(None)

    # mplfinance requires the index to be sorted ascending
    df = df.sort_index()

    # Keep only the four OHLCV columns (drop any extras)
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"DataFrame is missing required columns: {missing}")

    return df[required_cols]
