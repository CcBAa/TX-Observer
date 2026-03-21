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

import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt       # noqa: E402
import mplfinance as mpf              # noqa: E402
import pandas as pd                   # noqa: E402
import pytz                           # noqa: E402
from PIL import Image                 # noqa: E402  (Pillow — stitch panels)

logger = logging.getLogger("tx_observer.renderer")

TW_TZ = pytz.timezone("Asia/Taipei")


# ---------------------------------------------------------------------------
# CJK font — locate / download NotoSansTC and register with Matplotlib
# ---------------------------------------------------------------------------
_FONT_DIR  = Path(__file__).parent / "fonts"
_FONT_FILE = _FONT_DIR / "NotoSansTC-Regular.ttf"

# System font paths checked before downloading (Ubuntu / Debian common locations).
# TC-specific single-language OTF files are listed BEFORE the combined TTC so
# Matplotlib gets a clean Traditional-Chinese font instead of defaulting to the
# JP variant embedded in the TTC collection.
_SYSTEM_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJKtc-Regular.otf",   # TC-specific ← preferred
    "/usr/share/fonts/truetype/noto/NotoSansTC-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",     # combined TTC (JP default) ← last resort
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
]

# Magic bytes that identify a real font file (TTF / OTF / TTC)
_FONT_MAGIC = {b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf"}


def _is_valid_font(path: Path) -> bool:
    """Return True if *path* starts with a known font-file magic number."""
    try:
        with path.open("rb") as f:
            return f.read(4) in _FONT_MAGIC
    except OSError:
        return False


def _register(path: Path) -> "fm.FontProperties":
    """Register *path* with Matplotlib and return a FontProperties object."""
    fm.fontManager.addfont(str(path))
    prop = fm.FontProperties(fname=str(path))
    matplotlib.rcParams["font.family"] = prop.get_name()
    return prop


def _find_via_fc_list() -> "Path | None":
    """
    Use fc-list to dynamically locate a CJK-capable font installed on the system.
    Tries Traditional Chinese first, then generic Chinese, then Japanese (also CJK).
    """
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
            return None   # fc-list not available
    return None


def _ensure_cjk_font() -> "fm.FontProperties | None":
    """
    Locate a CJK-capable font and register it with Matplotlib.

    Search order:
      1. Local cached file (fonts/NotoSansTC-Regular.ttf) — magic-byte validated.
         Corrupt files (e.g. a previously saved HTML 404 page) are deleted.
      2. fc-list — dynamically queries fontconfig for installed CJK fonts.
      3. Static fallback paths for Ubuntu / Debian Noto CJK packages.

    If all sources fail the log shows the exact apt-get command to fix it.
    """
    # 1. Local cache (user-placed or previously downloaded valid file)
    if _FONT_FILE.exists():
        if _is_valid_font(_FONT_FILE):
            prop = _register(_FONT_FILE)
            logger.info("CJK font loaded from cache: %s  →  family '%s'",
                        _FONT_FILE.name, prop.get_name())
            return prop
        logger.warning(
            "Cached font file %s is not a valid font (possibly a corrupt download). "
            "Deleting it.", _FONT_FILE
        )
        _FONT_FILE.unlink()

    # 2. fc-list — works automatically after `apt-get install fonts-noto-cjk`
    fc_path = _find_via_fc_list()
    if fc_path is not None:
        prop = _register(fc_path)
        logger.info("CJK font found via fc-list: %s  →  family '%s'",
                    fc_path, prop.get_name())
        return prop

    # 3. Static system paths (belt-and-suspenders for known Ubuntu locations)
    for sys_path_str in _SYSTEM_FONT_CANDIDATES:
        sys_path = Path(sys_path_str)
        if sys_path.exists() and _is_valid_font(sys_path):
            prop = _register(sys_path)
            logger.info("CJK font found at static path: %s  →  family '%s'",
                        sys_path, prop.get_name())
            return prop

    logger.warning(
        "No CJK font found — Chinese characters will appear as boxes.\n"
        "  Fix: sudo apt-get install -y fonts-noto-cjk\n"
        "       then restart TX-Observer."
    )
    return None


_FONT_PROPS: "fm.FontProperties | None" = _ensure_cjk_font()
_FONT_FAMILY: str = _FONT_PROPS.get_name() if _FONT_PROPS is not None else "sans-serif"


# ---------------------------------------------------------------------------
# Market style — Taiwan convention: red = up (漲), green = down (跌)
# edge 與 wick 顯式設定，確保高解析度下影線清晰可見（不依賴 inherit 推斷）
# ---------------------------------------------------------------------------
_MARKET_COLORS = mpf.make_marketcolors(
    up="red",
    down="green",
    edge={"up": "red",   "down": "green"},   # K 棒邊框顏色與實體一致
    wick={"up": "red",   "down": "green"},   # 上下影線顏色與實體一致
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
# MA configuration — periods 5, 10, 20, 60 (Taiwan standard)
# ---------------------------------------------------------------------------
_MA_PERIODS = [5,         10,       20,       60,       240     ]
_MA_COLORS  = ["#FFD700", "#00BFFF", "#FF69B4", "#FFA500", "#FFFFFF"]
_MA_WIDTHS  = [1.0,       1.0,       1.0,       1.2,       1.5     ]

# Display window sizes — 手機高清辨識優先，根數減少讓 K 棒更寬
_5K_DISPLAY_BARS  = 90    # ~7.5 小時的 5 分 K（手機清晰辨識）
_60K_DISPLAY_BARS = 45    # ~45 根 60 分 K（約 2.25 個交易週）

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
    # figsize (12, 10): 寬 12" × 高 10"，搭配 dpi=300 輸出 3600×3000 px
    # 5K 面板略高（更多根數），60K 面板因根數少故略矮但仍清晰
    buf_5k  = _render_panel_to_buffer(
        df_5k,  symbol, "5K",  _5K_DISPLAY_BARS,  figsize=(12, 10)
    )
    buf_60k = _render_panel_to_buffer(
        df_60k, symbol, "60K", _60K_DISPLAY_BARS, figsize=(12, 8)
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
) -> io.BytesIO:
    """
    Render a single candlestick panel to a BytesIO PNG buffer.

    MAs are computed on the FULL dataset for accuracy, then the view
    is sliced to *display_bars* before plotting.

    Title is set via axes[0].set_title(loc='center') so it aligns with
    the K-line chart area rather than the full figure width.
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

    plot_kwargs: dict = dict(
        type="candle",
        style=_DARK_STYLE,
        volume=False,           # 不顯示成交量，讓 K 線圖佔滿垂直空間
        figsize=figsize,
        returnfig=True,
        warn_too_much_data=10_000,
        show_nontrading=False,
    )
    if addplots:
        plot_kwargs["addplot"] = addplots

    try:
        fig, axes = mpf.plot(df_display, **plot_kwargs)
        ax_main = axes[0]
        _color_doji_candles(ax_main, df_display)

        # ── 置中標題：[品種名稱]  5K / 60K ──────────────────────────────────
        title = f"[{symbol}]  {timeframe}"
        title_kw: dict = dict(loc="center", color="#e6edf3", fontsize=12, pad=8)
        if _FONT_PROPS is not None:
            title_kw["fontproperties"] = _FONT_PROPS
        ax_main.set_title(title, **title_kw)

        # ── 均線圖例：X 軸下方單行水平橫排，不遮擋 K 線 ───────────────────
        # bbox_to_anchor=(0.5, -0.15)：Axes 正下方外側置中
        # ncol=全數量：強制單行排列；frameon=False：移除背景邊框
        ma_handles = [l for l in ax_main.get_lines() if l.get_label().startswith("MA")]
        if ma_handles:
            leg = ax_main.legend(
                handles=ma_handles,
                loc="upper center",
                bbox_to_anchor=(0.5, -0.15),
                ncol=len(ma_handles),
                frameon=False,
                fontsize=9,
                handlelength=1.5,
                columnspacing=1.2,
            )
            for text in leg.get_texts():
                text.set_color("#e6edf3")
                if _FONT_PROPS is not None:
                    text.set_fontproperties(_FONT_PROPS)

        # tight_layout：極大化繪圖區域，為頂部標題預留 5% 空間
        try:
            fig.tight_layout(rect=[0, 0, 1, 0.95])
        except Exception:
            pass  # mplfinance 偶爾有 layout 警告，忽略即可

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=300, bbox_inches="tight")
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
    Each addplot carries a label so axes[0].legend() can pick them up later.
    """
    result = []
    for period, color, width in zip(_MA_PERIODS, _MA_COLORS, _MA_WIDTHS):
        col = f"MA{period}"
        if col not in df.columns:
            continue
        series = df[col]
        if series.notna().any():
            result.append(
                mpf.make_addplot(
                    series,
                    color=color,
                    width=width,
                    secondary_y=False,
                    label=f"MA{period}",
                )
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
