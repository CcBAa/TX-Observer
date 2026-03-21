"""
renderer.py — Headless K-line chart rendering for TX-Observer.

Public API
----------
render_combined_chart(df_5k, df_60k, symbol, output_dir) → Path
    Renders 5K and 60K charts into a single combined PNG image:
      - Upper panel (~56% height): 5K candlestick + MA lines
      - Shared legend row: single horizontal MA legend strip between panels
      - Lower panel (~44% height): 60K candlestick + MA lines

Design notes
------------
- Forces Agg backend (no GUI / display required)
- Taiwan colour convention: red = up (漲), green = down (跌)
- MA lines pre-computed on the FULL dataset so tail values are accurate
  after slicing to the display window
- Single matplotlib Figure with 2 GridSpec subplots; mplfinance plots
  candles via ax= external-axes mode; MA lines are overlaid manually
  with ax.plot() at integer x-coordinates (aligned to mplfinance's
  internal show_nontrading=False x-axis)
- One shared fig.legend() placed in the hspace gap between panels —
  no per-panel legend, no Pillow stitching required
"""

import logging
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # Must be set BEFORE any other matplotlib import

import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.lines as mlines     # noqa: E402
import matplotlib.pyplot as plt       # noqa: E402
import mplfinance as mpf              # noqa: E402
import pandas as pd                   # noqa: E402
import pytz                           # noqa: E402

logger = logging.getLogger("tx_observer.renderer")

TW_TZ = pytz.timezone("Asia/Taipei")


# ---------------------------------------------------------------------------
# CJK font — locate NotoSansTC and register with Matplotlib
# ---------------------------------------------------------------------------
_FONT_DIR  = Path(__file__).parent / "fonts"
_FONT_FILE = _FONT_DIR / "NotoSansTC-Regular.ttf"

_SYSTEM_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJKtc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansTC-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
]

_FONT_MAGIC = {b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf"}


def _is_valid_font(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(4) in _FONT_MAGIC
    except OSError:
        return False


def _register(path: Path) -> "fm.FontProperties":
    fm.fontManager.addfont(str(path))
    prop = fm.FontProperties(fname=str(path))
    matplotlib.rcParams["font.family"] = prop.get_name()
    return prop


def _find_via_fc_list() -> "Path | None":
    import subprocess
    for lang in ("zh-tw", "zh-hant", "zh", "ja"):
        try:
            out = subprocess.run(
                ["fc-list", f":lang={lang}", "--format=%{file}\n"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            for line in out.splitlines():
                p = Path(line.strip())
                if p.exists() and _is_valid_font(p):
                    return p
        except Exception:
            return None
    return None


def _ensure_cjk_font() -> "fm.FontProperties | None":
    if _FONT_FILE.exists():
        if _is_valid_font(_FONT_FILE):
            prop = _register(_FONT_FILE)
            logger.info("CJK font loaded from cache: %s  →  family '%s'",
                        _FONT_FILE.name, prop.get_name())
            return prop
        logger.warning("Cached font %s is corrupt — deleting.", _FONT_FILE)
        _FONT_FILE.unlink()

    fc_path = _find_via_fc_list()
    if fc_path is not None:
        prop = _register(fc_path)
        logger.info("CJK font via fc-list: %s  →  '%s'", fc_path, prop.get_name())
        return prop

    for sys_path_str in _SYSTEM_FONT_CANDIDATES:
        sys_path = Path(sys_path_str)
        if sys_path.exists() and _is_valid_font(sys_path):
            prop = _register(sys_path)
            logger.info("CJK font at static path: %s  →  '%s'",
                        sys_path, prop.get_name())
            return prop

    logger.warning(
        "No CJK font found — Chinese characters will appear as boxes.\n"
        "  Fix: sudo apt-get install -y fonts-noto-cjk"
    )
    return None


_FONT_PROPS: "fm.FontProperties | None" = _ensure_cjk_font()
_FONT_FAMILY: str = _FONT_PROPS.get_name() if _FONT_PROPS is not None else "sans-serif"


# ---------------------------------------------------------------------------
# Market style — Taiwan convention: red = up (漲), green = down (跌)
# edge 與 wick 顯式設定，確保高解析度下影線清晰可見
# ---------------------------------------------------------------------------
_MARKET_COLORS = mpf.make_marketcolors(
    up="red",
    down="green",
    edge={"up": "red",   "down": "green"},
    wick={"up": "red",   "down": "green"},
    ohlc="inherit",
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
        "font.family":      _FONT_FAMILY,
        "axes.labelcolor":  "#c9d1d9",
        "axes.edgecolor":   "#30363d",
        "xtick.color":      "#8b949e",
        "ytick.color":      "#8b949e",
        "figure.titlesize": 10,
        "text.color":       "#e6edf3",
    },
)

# ---------------------------------------------------------------------------
# MA configuration — periods 5, 10, 20, 60, 240 (Taiwan standard)
# ---------------------------------------------------------------------------
_MA_PERIODS = [5,         10,       20,       60,       240     ]
_MA_COLORS  = ["#FFD700", "#00BFFF", "#FF69B4", "#FFA500", "#FFFFFF"]
_MA_WIDTHS  = [1.0,       1.0,       1.0,       1.2,       1.5     ]

# Display window sizes
_5K_DISPLAY_BARS  = 90    # ~7.5 小時的 5 分 K
_60K_DISPLAY_BARS = 65    # ~65 根 60 分 K（約 3.25 個交易週）

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
    Single matplotlib Figure with 2 GridSpec rows:
      Row 0 (height 10): 5K candles + MA overlays
      Row 1 (height  8): 60K candles + MA overlays
      hspace gap between rows hosts the shared fig.legend()

    Parameters
    ----------
    df_5k      : OHLCV DataFrame at 5-min resolution.
    df_60k     : OHLCV DataFrame at 60-min resolution.
    symbol     : Display name, e.g. "台指期近一".
    output_dir : Output directory. Created if absent.

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

    # ── Data preparation ────────────────────────────────────────────────────
    df_5k_d  = _prepare_and_slice(df_5k,  symbol, "5K",  _5K_DISPLAY_BARS)
    df_60k_d = _prepare_and_slice(df_60k, symbol, "60K", _60K_DISPLAY_BARS)

    ohlcv = ["Open", "High", "Low", "Close", "Volume"]

    # ── Single figure, 2 subplots ───────────────────────────────────────────
    # figsize=(16, 12)：寬幅比例，讓 K 棒間距充足
    # hspace=0.50：留出足夠縫隙放中間橫排圖例，不擋 60K 標題
    fig = plt.figure(figsize=(16, 12), facecolor="#0d1117")
    gs  = fig.add_gridspec(2, 1, height_ratios=[10, 8], hspace=0.50)
    ax_5k  = fig.add_subplot(gs[0])
    ax_60k = fig.add_subplot(gs[1])

    # ── Candlestick via mplfinance (external-axes mode) ─────────────────────
    # volume=False：讓 K 線圖佔滿整個 Axes
    # show_nontrading=False：跳過非交易時段，x 座標為連續整數
    _mpf_kwargs = dict(
        type="candle",
        style=_DARK_STYLE,
        volume=False,
        warn_too_much_data=10_000,
        show_nontrading=False,
    )
    mpf.plot(df_5k_d[ohlcv],  ax=ax_5k,  **_mpf_kwargs)
    mpf.plot(df_60k_d[ohlcv], ax=ax_60k, **_mpf_kwargs)

    # ── X 軸時間刻度 ────────────────────────────────────────────────────────
    # 5K：每 60 分鐘一格；60K：每 10 小時（600 分鐘）一格
    _set_time_ticks(ax_5k,  df_5k_d,  interval_minutes=60)
    _set_time_ticks(ax_60k, df_60k_d, interval_minutes=600)

    # ── MA lines — overlaid manually at integer x-positions ─────────────────
    # mplfinance (show_nontrading=False) places bar i at x=i,
    # so range(len(df)) aligns exactly with each candle centre.
    for period, color, width in zip(_MA_PERIODS, _MA_COLORS, _MA_WIDTHS):
        col = f"MA{period}"
        for ax, df_d in ((ax_5k, df_5k_d), (ax_60k, df_60k_d)):
            s = df_d[col]
            if s.notna().any():
                ax.plot(
                    range(len(df_d)), s.values,
                    color=color, linewidth=width,
                    zorder=3, solid_capstyle="round",
                )

    # ── Doji highlighting ───────────────────────────────────────────────────
    _color_doji_candles(ax_5k,  df_5k_d)
    _color_doji_candles(ax_60k, df_60k_d)

    # ── Titles (置中，使用 NotoSansTC) ──────────────────────────────────────
    title_kw: dict = dict(loc="center", color="#e6edf3", fontsize=12, pad=8)
    if _FONT_PROPS is not None:
        title_kw["fontproperties"] = _FONT_PROPS
    ax_5k.set_title(f"[{symbol}]  5K",  **title_kw)
    ax_60k.set_title(f"[{symbol}]  60K", **title_kw)

    # ── Global legend — 單行水平，放在兩圖之間的縫隙 ────────────────────────
    # height_ratios=[10, 8]，hspace=0.50 → 縫隙幾何中心約在 Figure y ≈ 0.495
    handles = _build_legend_handles()
    leg = fig.legend(
        handles=handles,
        ncol=len(handles),
        loc="center",
        bbox_to_anchor=(0.5, 0.495),   # 兩圖縫隙的幾何中心
        frameon=False,
        fontsize=9,
        handlelength=1.5,
        columnspacing=1.2,
    )
    for text in leg.get_texts():
        text.set_color("#e6edf3")
        if _FONT_PROPS is not None:
            text.set_fontproperties(_FONT_PROPS)

    # ── Save ────────────────────────────────────────────────────────────────
    fig.savefig(str(filepath), dpi=300, bbox_inches="tight", facecolor="#0d1117")
    plt.close(fig)

    logger.info("Combined chart saved → %s", filepath)
    return filepath


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _prepare_and_slice(
    df:           pd.DataFrame,
    symbol:       str,
    timeframe:    str,
    display_bars: int,
) -> pd.DataFrame:
    """
    Normalise → validate → compute MAs on FULL dataset → slice to display window.

    MA computation on the full dataset ensures tail values match XQ exactly,
    regardless of how many bars are shown.
    """
    df_plot = _prepare_dataframe(df)
    df_plot = df_plot.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

    if len(df_plot) < 3:
        raise ValueError(
            f"[{symbol}] {timeframe}: only {len(df_plot)} bar(s) available "
            f"(need at least 3)."
        )

    # MA on full dataset BEFORE slicing
    for period in _MA_PERIODS:
        df_plot[f"MA{period}"] = df_plot["Close"].rolling(period).mean()

    df_display = df_plot.iloc[-display_bars:].copy()
    logger.info(
        "Rendering [%s] %s: displaying last %d of %d bars",
        symbol, timeframe, len(df_display), len(df_plot),
    )
    return df_display


def _set_time_ticks(
    ax,
    df:               pd.DataFrame,
    interval_minutes: int,
    fmt:              str = "%m-%d %H:%M",
) -> None:
    """
    Set x-axis ticks and labels at *interval_minutes* cadence.

    mplfinance (show_nontrading=False) assigns bar i the integer x-coordinate i,
    so matplotlib.dates locators cannot be used directly.  Instead we scan the
    DataFrame's DatetimeIndex for bars whose timestamp is an exact multiple of
    *interval_minutes* from midnight, then set those integer positions as ticks.

    Parameters
    ----------
    ax               : The Axes object returned from mpf.plot(ax=...).
    df               : The sliced display DataFrame (DatetimeIndex, tz-naive).
    interval_minutes : Tick cadence in minutes.
                       • 5K  panel → 60   (每 60 分鐘一格)
                       • 60K panel → 600  (每 10 小時一格)
    fmt              : strftime format for tick labels.
    """
    positions: list[int] = []
    labels:    list[str] = []

    for i, dt in enumerate(df.index):
        total_min = dt.hour * 60 + dt.minute
        if total_min % interval_minutes == 0:
            positions.append(i)
            labels.append(dt.strftime(fmt))

    # Fallback: evenly spaced if no bar falls on the exact interval
    if not positions:
        step = max(1, len(df) // 8)
        positions = list(range(0, len(df), step))
        labels    = [df.index[i].strftime(fmt) for i in positions]

    ax.set_xticks(positions)
    ax.set_xticklabels(labels, rotation=45, ha="right",
                       fontsize=8, color="#8b949e")
    # Vertical grid lines at every tick
    ax.xaxis.grid(True, linestyle="--", color="#2a2a3e", linewidth=0.7)


def _build_legend_handles() -> list:
    """
    Build proxy Line2D handles for the shared MA legend.
    Uses module-level constants directly — no plot object required.
    """
    return [
        mlines.Line2D([], [], color=color, linewidth=width, label=f"MA{period}")
        for period, color, width in zip(_MA_PERIODS, _MA_COLORS, _MA_WIDTHS)
    ]


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
