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
    up="red",           # Bullish candle — Taiwan convention (紅漲)
    down="green",       # Bearish candle — Taiwan convention (綠跌)
    inherit=True,       # Edge, wick, and volume bars all inherit up/down colours
)

_DARK_STYLE = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=_MARKET_COLORS,
    # mavcolors intentionally omitted — MAs are drawn via addplot (pre-computed
    # from full history) rather than the mav= parameter.
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
        "text.color":       "#e6edf3",
    },
)

# ---------------------------------------------------------------------------
# MA configuration
# MA lines are pre-computed on the FULL dataset so values at the display
# window tail are correct, then sliced together with the OHLCV data.
# ---------------------------------------------------------------------------
_MA_PERIODS = [5,        10,       20,       60,       240     ]
_MA_COLORS  = ["#FFD700","#00BFFF","#FF69B4","#FFA500","#C0C0C0"]
_MA_WIDTHS  = [1.0,      1.0,      1.0,      1.2,      1.2     ]

# How many candles to actually render per timeframe.
# The full dataset is still used for MA calculation above.
_DISPLAY_BARS: dict[str, int] = {
    "1K":  200,
    "5K":  120,   # ~1 full trading day of 5-min bars — matches XQ default view
    "60K":  80,   # ~4 trading weeks of hourly bars
}
_DISPLAY_BARS_DEFAULT = 120

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

    # Drop any rows with missing OHLCV values before MA computation so that
    # rolling() produces correct averages rather than propagating NaNs.
    df_plot = df_plot.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

    if len(df_plot) < 3:
        raise ValueError(
            f"DataFrame has only {len(df_plot)} row(s); at least 3 are required to render a chart."
        )

    # Warn if not enough bars for MA240
    if len(df_plot) < 240:
        logger.warning(
            "Only %d bars available — MA240 will be incomplete (need >= 240).",
            len(df_plot),
        )

    # ------------------------------------------------------------------
    # Pre-compute MAs on the FULL dataset
    # This is the critical step: rolling() sees all history, so the MA
    # values at the tail of the display window are correct even after slicing.
    # ------------------------------------------------------------------
    for period in _MA_PERIODS:
        df_plot[f"MA{period}"] = df_plot["Close"].rolling(period).mean()

    # ------------------------------------------------------------------
    # Build title (use last close from full dataset, before slicing)
    # ------------------------------------------------------------------
    latest_close = float(df_plot["Close"].iloc[-1])
    prev_close   = float(df_plot["Close"].iloc[-2]) if len(df_plot) > 1 else latest_close
    change       = latest_close - prev_close
    change_pct   = (change / prev_close) * 100.0 if prev_close else 0.0
    change_arrow = "▲" if change >= 0 else "▼"

    title_line1 = (
        f"TXF (台指期近一)  |  {timeframe}  |  "
        f"Last: {latest_close:,.0f}  "
        f"{change_arrow} {abs(change):.0f} ({change_pct:+.2f}%)"
    )
    title_line2 = (
        f"Generated: {now_tw.strftime('%Y-%m-%d %H:%M')} (UTC+8)  "
        f"|  MA: 5 (gold)  10 (blue)  20 (pink)  60 (orange)  240 (silver)"
    )
    title = f"{title_line1}\n{title_line2}"

    # ------------------------------------------------------------------
    # Slice to display window
    # ------------------------------------------------------------------
    display_bars = _DISPLAY_BARS.get(timeframe.upper(), _DISPLAY_BARS_DEFAULT)
    df_display   = df_plot.iloc[-display_bars:].copy()
    logger.info(
        "Rendering %s: displaying last %d of %d bars",
        timeframe, len(df_display), len(df_plot),
    )

    # ------------------------------------------------------------------
    # Build addplot list from pre-computed (and now sliced) MA columns
    # ------------------------------------------------------------------
    addplots = _build_ma_addplots(df_display)

    # Strip MA columns — mpf.plot() only wants OHLCV
    ohlcv_cols = ["Open", "High", "Low", "Close", "Volume"]
    df_display = df_display[ohlcv_cols]

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------
    plot_kwargs: dict = dict(
        type="candle",
        style=_DARK_STYLE,
        title=title,
        volume=True,
        figsize=(18, 10),
        savefig={"fname": str(filepath), "dpi": 150, "bbox_inches": "tight"},
        warn_too_much_data=10_000,
        show_nontrading=False,   # suppress weekend/holiday blank columns
    )
    if addplots:
        plot_kwargs["addplot"] = addplots

    try:
        mpf.plot(df_display, **plot_kwargs)
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

def _build_ma_addplots(df: pd.DataFrame) -> list:
    """
    Build a list of mplfinance addplot objects for the pre-computed MA columns
    in *df*.  Columns that are entirely NaN (not enough history for that period)
    are silently skipped.

    Parameters
    ----------
    df : pd.DataFrame
        Display-window slice that already contains MA{n} columns alongside OHLCV.
    """
    result = []
    for period, color, width in zip(_MA_PERIODS, _MA_COLORS, _MA_WIDTHS):
        col = f"MA{period}"
        if col not in df.columns:
            continue
        series = df[col]
        if series.notna().any():
            result.append(
                mpf.make_addplot(series, color=color, width=width, secondary_y=False)
            )
    return result


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
