"""
fetcher.py — Data fetching and resampling for TX-Observer.

Uses Shioaji (永豐金 API) to fetch 台指期貨近一連續合約 (TXFR1) 1-minute
OHLCV bars, then resamples to the requested timeframe using session-aware
aggregation.

Why session-aware resample?
    pandas resample() treats time as a continuous axis.  Without isolation,
    a 60-min bin can straddle the 13:45–15:00 gap and produce a phantom bar
    that mixes the afternoon close with the night-session open — causing the
    discrepancy visible between TX-Observer and XQ.

Public interface
----------------
fetcher = ShioajiDataFetcher()
df_5k  = fetcher.fetch_bars("5min",  bars=200)
df_60k = fetcher.fetch_bars("60min", bars=100)
"""

import atexit
import logging
import math
import os
from datetime import date, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import pytz
import shioaji as sj
from dotenv import load_dotenv

# pandas >= 1.1 replaced resample(base=) with resample(offset=)
# pandas >= 2.2 removed base= entirely
_PD_GTE_1_1 = tuple(int(x) for x in pd.__version__.split(".")[:2]) >= (1, 1)

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logger = logging.getLogger("tx_observer.fetcher")

TW_TZ = pytz.timezone("Asia/Taipei")


# ---------------------------------------------------------------------------
# TXF session windows (UTC+8 wall-clock times)
# ---------------------------------------------------------------------------
# Day session   : 08:45 – 13:45
# Night session : 15:00 – 05:00 (next day)
# Closed        : 05:01 – 08:44  and  13:46 – 14:59
_DAY_START   = dtime(8, 45)
_DAY_END     = dtime(13, 45)
_NIGHT_START = dtime(15, 0)
_NIGHT_END   = dtime(5, 0)

# Gap threshold for session-block detection.
# Any time-delta > this value between adjacent 1-min bars marks a new block.
_SESSION_GAP = pd.Timedelta(minutes=70)

# Conservative estimate: TXF 1-min bars per trading day
# Day 300 min + night tail ~180 min average = 480
_TRADING_MIN_PER_DAY = 480


def _is_in_session(t: dtime) -> bool:
    """Return True if bar timestamp *t* falls within a TXF trading session."""
    if _DAY_START <= t <= _DAY_END:
        return True
    if t >= _NIGHT_START:
        return True
    if t <= _NIGHT_END:
        return True
    return False


def _get_60k_label(ts: pd.Timestamp) -> pd.Timestamp:
    """
    Map a 1-min bar timestamp to its XQ-standard 60K bar label.

    XQ uses two different cut-points depending on the session:

    Day session (08:45–13:45) — cut at :46 past each hour
    ┌─────────────────────────────┬──────────┐
    │  1-min bars included        │  Label   │
    ├─────────────────────────────┼──────────┤
    │  08:45* → 09:45             │  09:45   │  * session open (special case)
    │  09:46  → 10:45             │  10:45   │
    │  10:46  → 11:45             │  11:45   │
    │  11:46  → 12:45             │  12:45   │
    │  12:46  → 13:45             │  13:45   │
    └─────────────────────────────┴──────────┘

    Night session (15:00–05:00) — cut at :01 past each hour
    ┌─────────────────────────────┬──────────┐
    │  1-min bars included        │  Label   │
    ├─────────────────────────────┼──────────┤
    │  15:00* → 16:00             │  16:00   │  * session open (special case)
    │  16:01  → 17:00             │  17:00   │
    │  …                          │  …       │
    │  23:01  → 00:00 (next day)  │  00:00   │
    │  00:01  → 01:00             │  01:00   │
    │  04:01  → 05:00             │  05:00   │
    └─────────────────────────────┴──────────┘

    Note: bars outside trading sessions are already filtered by _normalise(),
    so no additional boundary check is required here.
    """
    t   = ts.time()
    fl  = ts.floor("min")   # strip sub-minute precision (safe for any tz)

    # ── Day session: cut at :46 ───────────────────────────────────────────
    if _DAY_START <= t <= _DAY_END:
        if t.minute >= 46 or (t.hour == 8 and t.minute == 45):
            # Belongs to the bar whose RIGHT boundary is (hour+1):45
            return fl.replace(hour=t.hour + 1, minute=45)
        else:
            # Belongs to the bar whose RIGHT boundary is hour:45
            return fl.replace(minute=45)

    # ── Night session: cut at :01 ─────────────────────────────────────────
    else:
        if t.minute == 0 and t.hour != 15:
            # The :00 bar closes the current group — label is this :00
            return fl
        else:
            # minute >= 1, OR 15:00 (session open) → label is NEXT :00
            # Using arithmetic handles midnight crossing transparently.
            return (fl + pd.Timedelta(hours=1)).replace(minute=0)


# ===========================================================================
# Shioaji API singleton
# ===========================================================================

_api: "sj.Shioaji | None" = None


def _get_api() -> "sj.Shioaji":
    global _api
    if _api is None:
        _api = _create_and_login()
    return _api


def _create_and_login() -> "sj.Shioaji":
    api_key    = os.getenv("SHIOAJI_API_KEY", "").strip()
    secret_key = os.getenv("SHIOAJI_SECRET_KEY", "").strip()

    if not api_key or not secret_key:
        raise EnvironmentError(
            "SHIOAJI_API_KEY and/or SHIOAJI_SECRET_KEY are missing from .env"
        )

    api = sj.Shioaji()
    try:
        api.login(api_key=api_key, secret_key=secret_key)
        logger.info("Shioaji login successful.")
    except Exception as exc:
        logger.error("Shioaji login failed: %s", exc)
        raise RuntimeError(f"Shioaji login failed: {exc}") from exc

    # Sync contract list — call without arguments for broadest compatibility.
    # Some Shioaji versions lack sj.constant.ContractType; catch gracefully.
    try:
        api.fetch_contracts()
        logger.info("Shioaji fetch_contracts completed.")
    except Exception as exc:
        logger.warning("fetch_contracts 失敗，將嘗試直接存取合約: %s", exc)

    atexit.register(_logout, api)
    return api


def _logout(api: "sj.Shioaji") -> None:
    try:
        api.logout()
        logger.info("Shioaji logout completed.")
    except Exception as exc:
        logger.warning("Shioaji logout warning: %s", exc)


# ===========================================================================
# Internal helpers
# ===========================================================================

def _normalise(kbars) -> pd.DataFrame:
    """
    Convert a Shioaji Kbars object to a clean 1-min OHLCV DataFrame.

    1. Unpack kbars → DataFrame
    2. Normalise column names (lowercase → Title-Case)
    3. ts → tz-aware Asia/Taipei DatetimeIndex
       Shioaji ts is nanoseconds of CST wall-clock time (not UTC); we
       localise directly to Asia/Taipei without UTC conversion.
    4. Filter to TXF session windows
    5. Drop zero-volume placeholder bars
    6. Sort ascending, promote index to 'Datetime' column
    """
    df = pd.DataFrame({**kbars})
    if df.empty:
        return df

    df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={
        "open":   "Open",
        "high":   "High",
        "low":    "Low",
        "close":  "Close",
        "volume": "Volume",
    })

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing  = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Shioaji kbars response is missing columns: {missing}")

    ts_col = df["ts"]
    if pd.api.types.is_integer_dtype(ts_col):
        dt_index = pd.to_datetime(ts_col, unit="ns").dt.tz_localize("Asia/Taipei")
    else:
        dt_index = pd.to_datetime(ts_col).dt.tz_localize("Asia/Taipei")

    df["Datetime"] = dt_index
    df = df.set_index("Datetime")[required].copy()

    # Filter to TXF trading sessions
    session_mask = pd.Series(df.index.time, index=df.index).apply(_is_in_session)
    df = df[session_mask.values]

    # Drop Shioaji zero-volume placeholder bars
    df = df[df["Volume"] > 0]

    df = df.dropna(subset=["Close"]).sort_index()
    df.index.name = "Datetime"
    return df.reset_index()


def _resample_session_aware(df_1min: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Resample 1-min OHLCV to *timeframe* with session isolation, aligned to XQ.

    Bin convention (closed='right', label='right')
    -----------------------------------------------
    - 5-min bar labelled 08:50 contains 1-min bars 08:46–08:50.
    - The session-opening 1-min bar (08:45) falls into the bin (08:40, 08:45]
      labelled 08:45 — one candle, matching XQ's first bar of the day.
    - 5-min: 45 % 5 == 0, so bins naturally land on :45, :50, :55 …
      → no offset adjustment needed.
    - 60-min: default bins land on :00 (08:00, 09:00, …).
      → offset='45min' (pandas ≥ 1.1) or base=45 (older) shifts them to
        :45, giving bins (07:45, 08:45], (08:45, 09:45], … as XQ expects.

    Session isolation
    -----------------
    A new block starts whenever the gap between adjacent 1-min bars exceeds
    _SESSION_GAP (70 min), covering both the 05:00–08:45 and 13:45–15:00
    closed windows.  Each block is resampled independently so no bin ever
    straddles a session break.

    Parameters
    ----------
    df_1min   : DataFrame with 'Datetime' column (tz-aware Asia/Taipei).
    timeframe : Pandas offset alias: "5min", "15min", "30min", "60min" …
    """
    df = df_1min.copy()

    if "Datetime" in df.columns:
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    # ── Step 1: assign session block IDs ──────────────────────────────────
    time_diffs = df.index.to_series().diff()
    new_block  = (time_diffs > _SESSION_GAP) | time_diffs.isna()
    df["_block"] = new_block.cumsum()

    agg_rules = {
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }

    # ── Build resample kwargs ──────────────────────────────────────────────
    # closed='right' + label='right': the right boundary is the bin label.
    #
    # offset='45min' aligns bins to TXF's 08:45 open:
    #   5-min  : 45 % 5 == 0, so default bins already hit :45.
    #            Adding offset='45min' is mathematically identical to the
    #            default but makes the intent explicit.
    #   60-min : default bins land at :00 (08:00, 09:00 …).
    #            offset='45min' shifts them to :45 (08:45, 09:45 …) ← required.
    rs_kwargs: dict = {"closed": "right", "label": "right"}
    if _PD_GTE_1_1:
        rs_kwargs["offset"] = "45min"       # pandas >= 1.1
    else:
        rs_kwargs["base"] = 45              # pandas < 1.1 (base in minutes)

    # ── Step 2: resample each block independently ──────────────────────────
    blocks: list[pd.DataFrame] = []
    for _, block_df in df.groupby("_block", sort=True):
        resampled = (
            block_df.drop(columns=["_block"])
            .resample(timeframe, **rs_kwargs)
            .agg(agg_rules)
        )
        # Drop empty bins (zero-volume rows that resample inserts as padding)
        resampled = resampled[resampled["Volume"] > 0].dropna(how="all")
        if not resampled.empty:
            blocks.append(resampled)

    if not blocks:
        return pd.DataFrame(columns=["Datetime", "Open", "High", "Low", "Close", "Volume"])

    # ── Step 3: concatenate and return ────────────────────────────────────
    df_out = pd.concat(blocks).sort_index()
    df_out.index.name = "Datetime"
    return df_out.reset_index()


def _resample_60min(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Resample 1-min OHLCV to 60-min bars using XQ's TXF-specific cut-points.

    This replaces pd.resample('60min') which would apply a uniform cut at
    :00 of each hour, misaligning both the day-session (should cut at :46)
    and the first bar of the night-session (15:00 must merge into 16:00).

    Implementation
    --------------
    1. Map every 1-min bar to its 60K label via _get_60k_label().
    2. groupby(label).agg(OHLCV rules) — one pass, no bin-padding artefacts.
    3. Drop zero-volume / all-NaN rows.

    MA lines MUST be calculated on the resulting 60K DataFrame, not on the
    1-min source.  The renderer already does this (rolling on the full df
    before slicing to the display window).
    """
    df = df_1min.copy()

    if "Datetime" in df.columns:
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    agg_rules = {
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }

    labels = df.index.map(_get_60k_label)

    df_out = (
        df.groupby(labels)
        .agg(agg_rules)
        .dropna(how="all")
    )
    df_out = df_out[df_out["Volume"] > 0]

    df_out.index.name = "Datetime"
    return df_out.sort_index().reset_index()


# ===========================================================================
# Public class
# ===========================================================================

class ShioajiDataFetcher:
    """
    Fetches 台指期貨近一連續合約 (TXFR1) K-line data from Shioaji.

    1-min bars are fetched from the exchange; resampling to 5-min or 60-min
    is done locally with session-isolated aggregation to avoid cross-gap bars.

    Usage
    -----
    fetcher = ShioajiDataFetcher()
    df_5k  = fetcher.fetch_bars("5min",  bars=200)
    df_60k = fetcher.fetch_bars("60min", bars=100)
    """

    _CONTRACT_CODE = "TXFR1"

    def __init__(self, symbol: str = _CONTRACT_CODE) -> None:
        self._symbol       = symbol
        self._latest_close: "float | None" = None

    # ------------------------------------------------------------------
    # Primary public method
    # ------------------------------------------------------------------

    def fetch_bars(
        self,
        timeframe: str = "5min",
        bars:      int  = 200,
        start:     "str | None" = None,
        end:       "str | None" = None,
    ) -> pd.DataFrame:
        """
        Fetch K-line bars at *timeframe* resolution.

        Internally fetches 1-min kbars (the only resolution supported by
        this Shioaji version) then resamples with session isolation.

        Parameters
        ----------
        timeframe : str   "1min", "5min", "10min", "15min", "30min", "60min".
        bars      : int   Maximum resampled bars to return (most recent N).
        start     : str   Date string "yyyy-mm-dd". Auto-derived if omitted.
        end       : str   Date string "yyyy-mm-dd". Defaults to today.

        Returns
        -------
        pd.DataFrame
            Columns: Datetime (tz-aware Asia/Taipei), Open, High, Low,
            Close, Volume.
        """
        supported = {"1min", "5min", "10min", "15min", "30min", "60min"}
        if timeframe not in supported:
            raise ValueError(
                f"Unsupported timeframe '{timeframe}'. Supported: {sorted(supported)}"
            )

        # How many 1-min bars do we need to produce *bars* resampled bars?
        min_per_bar   = int(timeframe.replace("min", "")) if timeframe != "1min" else 1
        needed_1min   = bars * min_per_bar
        # Add 50 % buffer for session gaps and non-trading minutes
        needed_1min   = int(needed_1min * 1.5)

        today = date.today()
        if end is None:
            end = today.strftime("%Y-%m-%d")
        if start is None:
            trading_days = math.ceil(needed_1min / _TRADING_MIN_PER_DAY)
            lookback     = math.ceil(trading_days * 7 / 5) + 10
            start = (today - timedelta(days=lookback)).strftime("%Y-%m-%d")

        api      = _get_api()
        contract = self._get_contract(api)

        logger.info(
            "Fetching 1-min kbars: TXFR1  %s → %s  (need ~%d 1-min bars for %d %s bars)",
            start, end, needed_1min, bars, timeframe,
        )

        try:
            kbars = api.kbars(contract, start=start, end=end)
        except Exception as exc:
            exc_str = str(exc).lower()
            if "permission" in exc_str or "unauthorized" in exc_str or "403" in exc_str:
                logger.error(
                    "❌ 期貨資料權限尚未開通！\n"
                    "   請至永豐金證券後台確認帳號已申請「期貨行情資料」權限。\n"
                    "   原始錯誤: %s",
                    exc,
                )
                raise PermissionError(
                    f"Shioaji 期貨資料權限不足 (Permission Denied): {exc}"
                ) from exc
            logger.error("api.kbars() raised: %s", exc)
            raise RuntimeError(f"Shioaji kbars fetch failed: {exc}") from exc

        df_1min = _normalise(kbars)

        if df_1min.empty:
            raise ValueError(
                f"No kbar data returned for TXFR1 ({start} → {end})"
            )

        logger.info("Fetched %d 1-min bars from exchange.", len(df_1min))

        # ── Resample ──────────────────────────────────────────────────────
        if timeframe == "1min":
            df_out = df_1min
        elif timeframe == "60min":
            # Custom groupby — day/night sessions use different cut-points
            df_out = _resample_60min(df_1min)
            logger.info(
                "Custom 60K groupby: %d bars (from %d 1-min bars)",
                len(df_out), len(df_1min),
            )
        else:
            # 5min and other timeframes: session-isolated resample with offset
            df_out = _resample_session_aware(df_1min, timeframe)
            logger.info(
                "Session-aware resample → %s: %d bars (from %d 1-min bars)",
                timeframe, len(df_out), len(df_1min),
            )

        if df_out.empty:
            raise ValueError(
                f"Resample to {timeframe} produced no bars for TXFR1 ({start} → {end})"
            )

        # Keep most recent N bars
        if len(df_out) > bars:
            df_out = df_out.iloc[-bars:].reset_index(drop=True)

        self._latest_close = float(df_out["Close"].iloc[-1])
        logger.info(
            "fetch_bars(%s): returning %d bars | latest close = %.0f",
            timeframe, len(df_out), self._latest_close,
        )
        return df_out

    def get_latest_price(self) -> float:
        """Return the most recently fetched close price."""
        if self._latest_close is None:
            df = self.fetch_bars("5min", bars=2)
            return float(df["Close"].iloc[-1])
        return self._latest_close

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_contract(api: "sj.Shioaji"):
        """Locate the TXFR1 contract object with a descriptive error on failure."""
        txf_group = getattr(api.Contracts.Futures, "TXF", None)
        contract  = getattr(txf_group, "TXFR1", None) if txf_group is not None else None

        if contract is None:
            available    = dir(txf_group) if txf_group is not None else []
            futures_hint = [a for a in available if a.startswith("TXF")]
            raise RuntimeError(
                "無法取得 TXFR1 合約物件。\n"
                f"  api.Contracts.Futures.TXF 下可見的合約: "
                f"{futures_hint or '(空，可能需要先呼叫 fetch_contracts)'}\n"
                "  請確認 Shioaji 版本並已成功登入。"
            )
        return contract


# ===========================================================================
# Backwards-compatible alias
# ===========================================================================

YFinanceDataFetcher = ShioajiDataFetcher


# ===========================================================================
# Standalone convenience function
# ===========================================================================

def fetch_data(
    timeframe: str = "5min",
    bars:      int  = 200,
    start:     "str | None" = None,
    end:       "str | None" = None,
) -> pd.DataFrame:
    """Fetch TXFR1 K-line data at the given resolution."""
    return ShioajiDataFetcher().fetch_bars(
        timeframe=timeframe, bars=bars, start=start, end=end
    )
