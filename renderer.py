"""
renderer.py — Headless K-line chart rendering for TX-Observer.

Public API
----------
render_combined_chart(df_5k, df_60k, symbol, output_dir) → Path
    Renders 5K and 60K charts into a single combined PNG image:
      - Upper panel (60% height): 5K candlestick + MA lines
      - Lower panel (40% height): 60K candlestick + MA lines

Design notes
------------
- Forces Agg backend (no GUI / display required)
- Taiwan colour convention: red = up (漲), green = down (跌)
- MA lines pre-computed on the FULL dataset so tail values are accurate
  after slicing to the display window
- Two panels are rendered independently by mplfinance (returnfig=True)
  then stitched vertically with Pillow — this avoids mplfinance external-axes
  compatibility quirks and produces identical styling for both panels
"""

import io
import logging
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # Must be set BEFORE any other matplotlib import

import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf          # noqa: E402
import pandas as pd               # noqa: E402
import pytz                       # noqa: E402
from PIL import Image             # noqa: E402  (Pillow — stitch panels)

logger = logging.getLogger("tx_observer.renderer")

TW_TZ = pytz.timezone("Asia/Taipei")

# ---------------------------------------------------------------------------
# Market style — Taiwan convention: red = up (漲), green = down (跌)
# ---------------------------------------------------------------------------
_MARKET_COLORS = mpf.make_marketcolors(
    up="red",
    down="green",
    inherit=True,
)

_DARK_STYLE = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=_MARKET_COLORS,
    gridstyle="--",
    gridcolor="#2a2a3e",
    facecolor="#0d1117",
    figcolor="#0d1117",
    y_on_right=True,
    rc={
        "font.size":        9,
        "axes.labelcolor":  "#c9d1d9",
        "axes.edgecolor":   "#30363d",
        "xtick.color":      "#8b949e",
        "ytick.color":      "#8b949e",
        "figure.titlesize": 10,
        "text.color":       "#e6edf3",
    },
)

# ---------------------------------------------------------------------------
# MA configuration — periods 5, 10, 20, 60 (Taiwan standard)
# ---------------------------------------------------------------------------
_MA_PERIODS = [5,         10,       20,       60      ]
_MA_COLORS  = ["#FFD700", "#00BFFF", "#FF69B4", "#FFA500"]
_MA_WIDTHS  = [1.0,       1.0,       1.0,       1.2     ]

# Display window sizes
_5K_DISPLAY_BARS  = 120   # ~1 full trading day of 5-min bars
_60K_DISPLAY_BARS = 80    # ~4 trading weeks of hourly bars

# Output directory
DEFAULT_OUTPUT_DIR = Path("charts")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_combined_chart(
    df_5k:      pd.DataFrame,
    df_60k:     pd.DataFrame,
    symbol:     str,
    output_dir: "Path | str" = DEFAULT_OUTPUT_DIR,
) -> Path:
    """
    Render 5K and 60K panels into a single combined PNG.

    Layout
    ------
    Upper panel (60% of total height): 5K candles + volume + MA(5,10,20,60)
    Lower panel (40% of total height): 60K candles + volume + MA(5,10,20,60)

    Parameters
    ----------
    df_5k      : OHLCV DataFrame at 5-min resolution (from fetcher).
    df_60k     : OHLCV DataFrame at 60-min resolution (from fetcher).
    symbol     : Display name shown in chart titles, e.g. "台指期近一".
    output_dir : Directory for the output PNG. Created if absent.

    Returns
    -------
    Path to the saved PNG file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    now_tw   = datetime.now(tz=TW_TZ)
    safe_sym = symbol.replace("/", "-").replace(" ", "_")
    filename = f"{safe_sym}_5k60k_{now_tw.strftime('%Y%m%d_%H%M')}.png"
    filepath = (output_dir / filename).resolve()

    logger.info("Rendering combined chart [%s] 5K+60K...", symbol)

    # Render each panel to an in-memory PNG buffer
    # figsize heights: 5K = 60% of 14", 60K = 40% of 14"
    buf_5k  = _render_panel_to_buffer(
        df_5k,  symbol, "5K",  _5K_DISPLAY_BARS,  figsize=(12, 8.4), now=now_tw
    )
    buf_60k = _render_panel_to_buffer(
        df_60k, symbol, "60K", _60K_DISPLAY_BARS, figsize=(12, 5.6), now=now_tw
    )

    # Stitch vertically
    img_5k  = Image.open(buf_5k)
    img_60k = Image.open(buf_60k)

    # Ensure both panels have the same width
    if img_5k.width != img_60k.width:
        img_60k = img_60k.resize(
            (img_5k.width, img_60k.height), Image.LANCZOS
        )

    combined = Image.new("RGB", (img_5k.width, img_5k.height + img_60k.height),
                         color=(13, 17, 23))
    combined.paste(img_5k,  (0, 0))
    combined.paste(img_60k, (0, img_5k.height))
    combined.save(str(filepath))

    for obj in (buf_5k, buf_60k, img_5k, img_60k, combined):
        obj.close()

    logger.info("Combined chart saved → %s", filepath)
    return filepath


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _render_panel_to_buffer(
    df:           pd.DataFrame,
    symbol:       str,
    timeframe:    str,
    display_bars: int,
    figsize:      "tuple[float, float]",
    now:          datetime,
) -> io.BytesIO:
    """
    Render a single candlestick panel to a BytesIO PNG buffer.

    MAs are computed on the FULL dataset for accuracy, then the view
    is sliced to *display_bars* before plotting.
    """
    df_plot = _prepare_dataframe(df)
    df_plot = df_plot.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

    if len(df_plot) < 3:
        raise ValueError(
            f"[{symbol}] {timeframe}: only {len(df_plot)} bar(s) available "
            f"(need at least 3)."
        )

    # Compute MAs on the full dataset BEFORE slicing
    for period in _MA_PERIODS:
        df_plot[f"MA{period}"] = df_plot["Close"].rolling(period).mean()

    # Price summary from the most recent bar (full dataset)
    latest = float(df_plot["Close"].iloc[-1])
    prev   = float(df_plot["Close"].iloc[-2]) if len(df_plot) > 1 else latest
    chg    = latest - prev
    pct    = (chg / prev * 100.0) if prev else 0.0
    arrow  = "▲" if chg >= 0 else "▼"

    # Slice to display window
    df_display = df_plot.iloc[-display_bars:].copy()

    logger.info(
        "Rendering [%s] %s: displaying last %d of %d bars",
        symbol, timeframe, len(df_display), len(df_plot),
    )

    # Build MA addplots from the sliced display DataFrame
    addplots = _build_ma_addplots(df_display)

    # Strip MA columns — mpf.plot() expects pure OHLCV
    ohlcv_cols = ["Open", "High", "Low", "Close", "Volume"]
    df_display = df_display[ohlcv_cols]

    # Build title
    if timeframe == "5K":
        title = (
            f"[{symbol}]  5K  ·  Last: {latest:,.0f}  "
            f"{arrow}{abs(chg):.0f} ({pct:+.2f}%)\n"
            f"MA: 5(金) · 10(藍) · 20(粉) · 60(橙)  "
            f"·  {now.strftime('%Y-%m-%d %H:%M')} UTC+8"
        )
    else:
        title = (
            f"[{symbol}]  60K  ·  Last: {latest:,.0f}  "
            f"{arrow}{abs(chg):.0f} ({pct:+.2f}%)\n"
            f"MA: 5(金) · 10(藍) · 20(粉) · 60(橙)"
        )

    plot_kwargs: dict = dict(
        type="candle",
        style=_DARK_STYLE,
        title=title,
        volume=True,
        figsize=figsize,
        returnfig=True,
        warn_too_much_data=10_000,
        show_nontrading=False,
    )
    if addplots:
        plot_kwargs["addplot"] = addplots

    try:
        fig, axes = mpf.plot(df_display, **plot_kwargs)
        _color_doji_candles(axes[0], df_display)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf

    except Exception as exc:
        plt.close("all")
        logger.error(
            "Failed to render [%s] %s panel: %s", symbol, timeframe, exc,
            exc_info=True,
        )
        raise RuntimeError(
            f"Chart rendering failed for [{symbol}] {timeframe}"
        ) from exc


def _build_ma_addplots(df: pd.DataFrame) -> list:
    """
    Build mplfinance addplot objects for the pre-computed MA columns in *df*.
    Columns that are entirely NaN (insufficient history) are silently skipped.
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


def _color_doji_candles(ax, df: pd.DataFrame, color: str = "#FFD700") -> None:
    """Recolour doji candle bodies (Open == Close) to *color* (gold)."""
    patches = ax.patches
    if not patches:
        return
    opens  = df["Open"].values
    closes = df["Close"].values
    for i, (o, c) in enumerate(zip(opens, closes)):
        if o == c and i < len(patches):
            patches[i].set_facecolor(color)
            patches[i].set_edgecolor(color)


def _prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalise the input DataFrame for mplfinance:
      - Ensures a proper DatetimeIndex
      - Converts tz-aware index to Asia/Taipei then strips tz-info
    """
    df = df.copy()

    if "Datetime" in df.columns:
        df = df.set_index("Datetime")

    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    if df.index.tz is not None:
        df.index = df.index.tz_convert(TW_TZ).tz_localize(None)

    df = df.sort_index()

    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"DataFrame is missing required columns: {missing}")

    return df[required_cols]
