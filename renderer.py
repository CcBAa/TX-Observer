"""
renderer.py — Headless K-line chart rendering for TX-Observer.

Public API
----------
render_combined_chart(df_upper, df_60k, symbol, output_dir, mode) → Path
    Renders two panels into a single combined PNG.

    mode="futures" (TXFR1)
    ──────────────────────
      Top panel  (~60%, Row 0): 60K trend chart  — last 65 bars
      Bot panel  (~40%, Row 1): 5K  signal chart — last 90 bars
      X-axis: top = every 10 h | bot = every 60 min
      Logic: large timeframe on top defines the trend;
             small timeframe below shows entry signals.

    mode="spot" (TSE/001, OTC/101)
    ───────────────────────────────
      Top panel  (~56%, Row 0): 日K  swing chart — last 45 bars
      Bot panel  (~44%, Row 1): 60K  trend chart — last 65 bars
      X-axis: top = %y-%m-%d labels | bot = every 10 h

    Shared between both modes
    ─────────────────────────
      - Global legend strip between the two panels (MA5/10/20/60/240)
      - Dark theme, dpi=300, Taiwan red=up / green=down convention
      - MA lines computed on the full fetched dataset (tail values accurate)
      - Highest / lowest price annotations per panel

Design notes
------------
- Forces Agg backend (no GUI / display required)
- Single matplotlib Figure with 2 GridSpec rows; mplfinance external-axes
  mode for candles; MA lines overlaid manually at integer x-coordinates
- One shared fig.legend() placed in the hspace gap — no Pillow stitching
"""

import gc
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
            continue
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
_5K_DISPLAY_BARS    = 90    # 台指期 5K  顯示根數（約 7.5 小時）
_DAILY_DISPLAY_BARS = 45    # 現貨  日K  顯示根數（約兩個月）
_60K_DISPLAY_BARS   = 65    # 共用  60K  顯示根數（約 3.25 週）

DEFAULT_OUTPUT_DIR = Path("charts")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_combined_chart(
    df_upper:   pd.DataFrame,
    df_60k:     pd.DataFrame,
    symbol:     str,
    output_dir: "Path | str" = DEFAULT_OUTPUT_DIR,
    mode:       str = "futures",
) -> Path:
    """
    Render two K-line panels into a single combined PNG.

    Panel assignment by mode
    ────────────────────────
    mode="futures" (TXFR1)
      Top  [60%]: 60K — 65 bars, tick every 600 min, title "[symbol] 60K"
      Bot  [40%]: 5K  — 90 bars, tick every  60 min, title "[symbol] 5K"

    mode="spot" (TSE/001, OTC/101)
      Top  [56%]: 日K — 45 bars, tick per ~5 bars (%y-%m-%d), title "[symbol] 日K"
      Bot  [44%]: 60K — 65 bars, tick every 600 min, title "[symbol] 60K"

    Parameters
    ----------
    df_upper   : OHLCV DataFrame.
                 mode="futures" → 5-min bars.
                 mode="spot"    → daily bars (pre-aggregated in main.py).
    df_60k     : OHLCV DataFrame at 60-min resolution.
    symbol     : Display name, e.g. "台指期近一" or "加權指數".
    output_dir : Output directory. Created if absent.
    mode       : "futures" or "spot".

    Returns
    -------
    Path to the saved PNG file.
    """
    if mode not in ("futures", "spot"):
        raise ValueError(f"render_combined_chart: unknown mode '{mode}'. "
                         "Use 'futures' or 'spot'.")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    now_tw   = datetime.now(tz=TW_TZ)
    safe_sym = symbol.replace("/", "-").replace(" ", "_")

    # ── Per-mode configuration ───────────────────────────────────────────────
    if mode == "futures":
        # Top = 60K trend  |  Bot = 5K signal
        height_ratios   = [6, 4]           # 60% / 40%
        file_tag        = "60k5k"
        top_label       = "60K"
        bot_label       = "5K"
        top_display     = _60K_DISPLAY_BARS   # 65
        bot_display     = _5K_DISPLAY_BARS    # 90
    else:
        # Top = 日K swing  |  Bot = 60K trend
        height_ratios   = [10, 8]          # ~56% / ~44%
        file_tag        = "dayk60k"
        top_label       = "日K"
        bot_label       = "60K"
        top_display     = _DAILY_DISPLAY_BARS  # 45
        bot_display     = _60K_DISPLAY_BARS    # 65

    filename = f"{safe_sym}_{file_tag}_{now_tw.strftime('%Y%m%d_%H%M')}.png"
    filepath = (output_dir / filename).resolve()

    logger.info(
        "Rendering combined chart [%s] %s(top)+%s(bot) | mode=%s",
        symbol, top_label, bot_label, mode,
    )

    # ── Data preparation ─────────────────────────────────────────────────────
    # Prepare both datasets.  For futures, 60K goes to the top axis;
    # for spot, df_upper (日K) goes to the top axis.
    if mode == "futures":
        df_top_d = _prepare_and_slice(df_60k,   symbol, top_label, top_display)
        df_bot_d = _prepare_and_slice(df_upper, symbol, bot_label, bot_display)
    else:
        df_top_d = _prepare_and_slice(df_upper, symbol, top_label, top_display)
        df_bot_d = _prepare_and_slice(df_60k,   symbol, bot_label, bot_display)

    ohlcv = ["Open", "High", "Low", "Close", "Volume"]

    # ── Single figure, 2 subplots ────────────────────────────────────────────
    # hspace=0.60 keeps the gap wide enough for the shared legend without
    # overlapping either panel's x-axis tick labels.
    fig = plt.figure(figsize=(16, 12), facecolor="#0d1117")
    fig.patch.set_facecolor("#0d1117")
    gs      = fig.add_gridspec(2, 1, height_ratios=height_ratios, hspace=0.60)
    ax_top  = fig.add_subplot(gs[0])
    ax_bot  = fig.add_subplot(gs[1])

    # ── Candlestick via mplfinance (external-axes mode) ──────────────────────
    _mpf_kwargs = dict(
        type="candle",
        style=_DARK_STYLE,
        volume=False,
        warn_too_much_data=10_000,
        show_nontrading=False,
    )
    mpf.plot(df_top_d[ohlcv], ax=ax_top, **_mpf_kwargs)
    mpf.plot(df_bot_d[ohlcv], ax=ax_bot, **_mpf_kwargs)

    # ── 深色主題修復 ──────────────────────────────────────────────────────────
    # external-axes 模式下 mplfinance 不自動套用 facecolor/tick 顏色，
    # 必須在 mpf.plot() 之後手動覆寫。
    _apply_dark_style(ax_top)
    _apply_dark_style(ax_bot)

    # ── X 軸時間刻度 ─────────────────────────────────────────────────────────
    # futures : top=60K(每 10 h)  bot=5K(每 60 min)
    # spot    : top=日K(每~5根)   bot=60K(每 10 h)
    if mode == "futures":
        _set_time_ticks(ax_top, df_top_d, interval_minutes=600)
        _set_time_ticks(ax_bot, df_bot_d, interval_minutes=60)
    else:
        _set_daily_ticks(ax_top, df_top_d, fmt="%y-%m-%d")
        _set_time_ticks(ax_bot, df_bot_d, interval_minutes=600)

    # ── MA lines — overlaid manually at integer x-positions ──────────────────
    for period, color, width in zip(_MA_PERIODS, _MA_COLORS, _MA_WIDTHS):
        col = f"MA{period}"
        for ax, df_d in ((ax_top, df_top_d), (ax_bot, df_bot_d)):
            s = df_d[col]
            if s.notna().any():
                ax.plot(
                    range(len(df_d)), s.values,
                    color=color, linewidth=width,
                    zorder=3, solid_capstyle="round",
                )

    # ── Doji highlighting ─────────────────────────────────────────────────────
    _color_doji_candles(ax_top, df_top_d)
    _color_doji_candles(ax_bot, df_bot_d)

    # ── 最高/最低價標注 ───────────────────────────────────────────────────────
    _annotate_high_low(ax_top, df_top_d)
    _annotate_high_low(ax_bot, df_bot_d)

    # ── Titles (置中，使用 NotoSansTC) ───────────────────────────────────────
    title_kw: dict = dict(loc="center", color="#e6edf3", fontsize=12, pad=8)
    if _FONT_PROPS is not None:
        title_kw["fontproperties"] = _FONT_PROPS
    ax_top.set_title(f"[{symbol}]  {top_label}", **title_kw)
    ax_bot.set_title(f"[{symbol}]  {bot_label}", **title_kw)

    # ── Global legend — 單行水平，放在兩圖縫隙中央 ───────────────────────────
    # bbox_to_anchor y=0.45 在兩種 height_ratios 下均落在縫隙中央略下方，
    # 確保不壓到上圖 x 軸標籤也不壓到下圖標題。
    handles = _build_legend_handles()
    leg = fig.legend(
        handles=handles,
        ncol=5,           # MA5 / MA10 / MA20 / MA60 / MA240 固定 5 欄水平排列
        loc="center",
        bbox_to_anchor=(0.5, 0.45),
        frameon=False,
        fontsize=9,
        handlelength=1.5,
        columnspacing=1.2,
    )
    for text in leg.get_texts():
        text.set_color("#e6edf3")
        if _FONT_PROPS is not None:
            text.set_fontproperties(_FONT_PROPS)

    # ── Save ─────────────────────────────────────────────────────────────────
    fig.savefig(str(filepath), dpi=300, bbox_inches="tight", facecolor="#0d1117")
    fig.clf()           # 清除 figure 內所有 axes / artists，釋放 Agg raster buffer
    plt.close(fig)
    plt.close("all")   # 清除所有殘留 figure 物件（防止 mplfinance 內部 figure 洩漏）
    gc.collect()        # 強制回收 Agg 大型 raster buffer，避免 render 後記憶體殘留

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

    # MA computation — skip if fetcher.py already pre-computed on a full buffer.
    # Pre-computed columns survive _prepare_dataframe() (which now preserves them).
    # Recomputing on a small display slice (e.g. 45 bars) would produce mostly-NaN
    # MA240; using fetcher's values computed on ≥300 bars is always more accurate.
    if f"MA{_MA_PERIODS[0]}" in df_plot.columns:
        logger.debug(
            "[%s] %s: MA 欄位由 fetcher 預計算，略過重新計算。", symbol, timeframe
        )
    else:
        for period in _MA_PERIODS:
            df_plot[f"MA{period}"] = df_plot["Close"].rolling(period).mean()

    df_display = df_plot.iloc[-display_bars:].copy()
    logger.info(
        "Rendering [%s] %s: displaying last %d of %d bars",
        symbol, timeframe, len(df_display), len(df_plot),
    )
    return df_display


def _apply_dark_style(ax) -> None:
    """
    Manually restore dark-theme colours on an external Axes object.

    When mplfinance plots into an externally-created Axes (ax= mode),
    the style's facecolor / tick colours / spine colours are NOT
    applied automatically.  This function sets them explicitly so the
    panel matches the project's dark theme.

    Call this AFTER mpf.plot() so we override anything mplfinance reset.
    """
    BG    = "#0d1117"
    TICK  = "#8b949e"
    SPINE = "#30363d"
    LABEL = "#c9d1d9"
    GRID  = "#2a2a3e"

    ax.set_facecolor(BG)

    for spine in ax.spines.values():
        spine.set_edgecolor(SPINE)

    ax.tick_params(axis="both", colors=TICK, which="both", labelsize=8)
    ax.xaxis.label.set_color(LABEL)
    ax.yaxis.label.set_color(LABEL)

    # Price labels on the right side
    ax.yaxis.set_label_position("right")
    ax.yaxis.tick_right()
    ax.tick_params(axis="y", labelcolor=TICK)

    # Horizontal grid lines (vertical lines added by tick helpers)
    ax.yaxis.grid(True, linestyle="--", color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)


def _annotate_high_low(ax, df: pd.DataFrame) -> None:
    """
    Annotate the highest High (▲) and lowest Low (▼) in the display window.

    Uses integer x-coordinates that match mplfinance's internal axis.
    Offsets the label slightly so it does not overlap the wick.
    """
    hi_pos = int(df["High"].values.argmax())
    lo_pos = int(df["Low"].values.argmin())
    hi_val = float(df["High"].iloc[hi_pos])
    lo_val = float(df["Low"].iloc[lo_pos])

    n = len(df)
    hi_ha = "right" if hi_pos > n * 0.85 else "left"
    lo_ha = "right" if lo_pos > n * 0.85 else "left"

    ax.annotate(
        f"▲ {hi_val:.0f}",
        xy=(hi_pos, hi_val),
        xytext=(-4 if hi_ha == "right" else 4, 4),
        textcoords="offset points",
        ha=hi_ha, va="bottom",
        color="#FF6B6B", fontsize=8, fontweight="bold", zorder=6,
    )
    ax.annotate(
        f"▼ {lo_val:.0f}",
        xy=(lo_pos, lo_val),
        xytext=(-4 if lo_ha == "right" else 4, -4),
        textcoords="offset points",
        ha=lo_ha, va="top",
        color="#51CF66", fontsize=8, fontweight="bold", zorder=6,
    )


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
    ax               : The Axes object (mplfinance external-axes mode).
    df               : Sliced display DataFrame (DatetimeIndex, tz-naive).
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
    ax.xaxis.grid(True, linestyle="--", color="#2a2a3e", linewidth=0.7)


def _set_daily_ticks(
    ax,
    df:  pd.DataFrame,
    fmt: str = "%y-%m-%d",
) -> None:
    """
    Set x-axis ticks for daily-K bars.

    Since daily bars have timestamps at midnight (no meaningful intraday
    minute to modulo against), ticks are placed at a fixed bar-count
    interval so roughly 9 labels appear across the display window.

    Parameters
    ----------
    ax  : The Axes object (mplfinance external-axes mode).
    df  : Sliced daily display DataFrame (DatetimeIndex, tz-naive midnight).
    fmt : strftime format — default "%y-%m-%d".
    """
    n    = len(df)
    step = max(1, n // 9)   # ~9 刻度均勻分布

    positions = list(range(0, n, step))
    labels    = [df.index[i].strftime(fmt) for i in positions]

    ax.set_xticks(positions)
    ax.set_xticklabels(labels, rotation=45, ha="right",
                       fontsize=8, color="#8b949e")
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

    # Preserve pre-computed MA columns from fetcher.py so _prepare_and_slice
    # can detect them and skip redundant rolling computation.
    ma_cols = sorted(
        [c for c in df.columns if c.startswith("MA") and c[2:].isdigit()],
        key=lambda x: int(x[2:]),
    )
    return df[required_cols + ma_cols]
