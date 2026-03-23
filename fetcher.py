"""
fetcher.py — Data fetching and resampling for TX-Observer.

Supports three symbols:
  - TXFR1   : 台指期貨近一連續合約 (futures, day + night session)
  - TSE/001 : 加權指數 (spot, 09:00–13:30)
  - OTC/101 : 櫃買指數 (spot, 09:00–13:30)

All symbols share the same Shioaji API singleton — login happens once at
process startup via init_api() / _get_api().

Resample convention
-------------------
TXFR1 5K  : session-isolated resample with offset='45min' (XQ-standard bins)
TXFR1 60K : custom groupby using _get_60k_label() — different cut-points for
             day (cut at :46) and night (cut at :01) sessions
Spot  5K  : session-isolated resample, standard bins (no offset)
Spot  60K : custom groupby using _get_60k_label_spot() — cut at :01 / 13:30
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

# 嘗試匯入 Shioaji 的 TokenError，以便精確捕獲 401 過期錯誤
try:
    from shioaji.error import TokenError as _SjTokenError
except ImportError:
    _SjTokenError = None  # type: ignore[assignment,misc]

# pandas >= 1.1 replaced resample(base=) with resample(offset=)
_PD_GTE_1_1 = tuple(int(x) for x in pd.__version__.split(".")[:2]) >= (1, 1)

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logger = logging.getLogger("tx_observer.fetcher")

TW_TZ = pytz.timezone("Asia/Taipei")


# ---------------------------------------------------------------------------
# TXF futures session windows (UTC+8)
# ---------------------------------------------------------------------------
_DAY_START   = dtime(8, 45)
_DAY_END     = dtime(13, 45)
_NIGHT_START = dtime(15, 0)
_NIGHT_END   = dtime(5, 0)

# Spot index session window (TSE / OTC, UTC+8)
_SPOT_DAY_START = dtime(9, 0)
_SPOT_DAY_END   = dtime(13, 30)

# Gap threshold for session-block detection
_SESSION_GAP = pd.Timedelta(minutes=70)

# Conservative estimate: TXF 1-min bars per trading day
_TRADING_MIN_PER_DAY = 480

# Spot symbols → (Shioaji market group, contract code)
_SPOT_SYMBOLS: dict[str, tuple[str, str]] = {
    "TSE/001": ("TSE", "TSE001"),
    "OTC/101": ("OTC", "OTC101"),
}


# ===========================================================================
# Session classification helpers
# ===========================================================================

def _get_trading_session(t: dtime) -> "str | None":
    """
    TXF trading session classifier.
    Returns 'day' (08:45–13:45), 'night' (15:00–05:00), or None (closed).
    """
    if _DAY_START <= t <= _DAY_END:
        return "day"
    if t >= _NIGHT_START:
        return "night"
    if t <= _NIGHT_END:
        return "night"
    return None


def _get_spot_trading_session(t: dtime) -> "str | None":
    """TSE/OTC spot index session: 09:00–13:30 only."""
    if _SPOT_DAY_START <= t <= _SPOT_DAY_END:
        return "day"
    return None


# ===========================================================================
# 60K label functions (XQ-standard cut points)
# ===========================================================================

def _get_60k_label(ts: pd.Timestamp) -> pd.Timestamp:
    """
    Map a TXF 1-min bar to its XQ-standard 60K bar label.

    Day session (cut at :46):
      08:45 → 09:45,  09:46 → 10:45,  …,  12:46 → 13:45

    Night session (cut at :01):
      15:00 → 16:00,  16:01 → 17:00,  …,  04:01 → 05:00
    """
    t  = ts.time()
    fl = ts.floor("min")

    session = _get_trading_session(t)
    if session is None:
        return pd.NaT

    if session == "day":
        if t.minute >= 46 or (t.hour == 8 and t.minute == 45):
            return fl.replace(hour=t.hour + 1, minute=45)
        else:
            return fl.replace(minute=45)
    else:
        if t.minute == 0 and t.hour != 15:
            return fl
        else:
            return (fl + pd.Timedelta(hours=1)).replace(minute=0)


def _get_60k_label_spot(ts: pd.Timestamp) -> pd.Timestamp:
    """
    Map a TSE/OTC spot index 1-min bar to its 60K label.

    Cut convention (cut at :01 → :00, session open grouped into first bar):
      09:00 → 10:00  (session open included in first full hour)
      09:01–10:00  → 10:00
      10:01–11:00  → 11:00
      11:01–12:00  → 12:00
      12:01–13:00  → 13:00
      13:01–13:30  → 13:30  (partial last bar)
    """
    t  = ts.time()
    fl = ts.floor("min")

    if not (_SPOT_DAY_START <= t <= _SPOT_DAY_END):
        return pd.NaT

    # Partial last bar: 13:01–13:30 → label 13:30
    if dtime(13, 0) < t <= dtime(13, 30):
        return fl.replace(hour=13, minute=30)

    # :00 bar closes the current group (except 09:00 which opens the session)
    if t.minute == 0 and t.hour != 9:
        return fl
    else:
        # 09:00 (session open) or minute >= 1 → next :00
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


def init_api() -> None:
    """Eagerly initialize the Shioaji API singleton. Call once at startup."""
    _get_api()


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


def _is_token_error(exc: Exception) -> bool:
    """判斷例外是否為 Token 過期 / 401 認證失敗。"""
    if _SjTokenError is not None and isinstance(exc, _SjTokenError):
        return True
    msg = str(exc).lower()
    return "401" in msg or ("token" in msg and "expir" in msg)


def _force_relogin() -> "sj.Shioaji":
    """
    強制重新建立 API 連線（跨週末或長時間閒置後 Token 過期時使用）。

    清除舊的 singleton、嘗試登出舊 session，然後重新執行 login + fetch_contracts。
    回傳新的 Shioaji 實例，後續所有任務均共用此新 singleton。
    """
    global _api
    logger.warning("偵測到 Token 過期或連線中斷，執行自動重新登入...")
    if _api is not None:
        try:
            _api.logout()
        except Exception as exc:
            logger.debug("重登入前舊 session 登出失敗（可忽略）: %s", exc)
    _api = None
    new_api = _get_api()
    logger.info("自動重新登入成功，新 Token 已就緒。")
    return new_api


# ===========================================================================
# Data normalisation
# ===========================================================================

def _normalise(kbars) -> pd.DataFrame:
    """
    Convert Shioaji Kbars to a clean 1-min OHLCV DataFrame (TXF futures).
    Filters to TXF session windows and drops zero-volume bars.
    """
    df = pd.DataFrame({**kbars})
    if df.empty:
        return df

    df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
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

    # Filter to TXF session windows
    session_mask = pd.Series(df.index.time, index=df.index).apply(
        lambda t: _get_trading_session(t) is not None
    )
    df = df[session_mask.values]
    df = df[df["Volume"] > 0]

    df = df.dropna(subset=["Close"]).sort_index()
    df.index.name = "Datetime"
    return df.reset_index()


def _normalise_spot(kbars) -> pd.DataFrame:
    """
    Convert Shioaji Kbars to a clean 1-min OHLCV DataFrame (spot index).
    Filters to 09:00–13:30 session.
    """
    df = pd.DataFrame({**kbars})
    if df.empty:
        return df

    df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
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

    # Filter to spot session (09:00–13:30)
    session_mask = pd.Series(df.index.time, index=df.index).apply(
        lambda t: _get_spot_trading_session(t) is not None
    )
    df = df[session_mask.values]

    # Drop zero-volume bars only if the column has any non-zero values
    # (some index contracts return volume; others may not)
    if df["Volume"].sum() > 0:
        df = df[df["Volume"] > 0]

    df = df.dropna(subset=["Close"]).sort_index()
    df.index.name = "Datetime"
    return df.reset_index()


# ===========================================================================
# Resample functions
# ===========================================================================

def _resample_session_aware(df_1min: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Resample TXF 1-min OHLCV to *timeframe* with session isolation.

    Uses offset='45min' (pandas ≥ 1.1) to align bins to TXF's 08:45 open.
    Blocks are split at session gaps > _SESSION_GAP so no bin straddles
    the 05:00–08:45 or 13:45–15:00 closed windows.
    """
    df = df_1min.copy()
    if "Datetime" in df.columns:
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    # Belt-and-suspenders: filter to TXF session
    session_mask = pd.Series(df.index.time, index=df.index).apply(
        lambda t: _get_trading_session(t) is not None
    )
    df = df[session_mask.values]
    if df.empty:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    agg_rules = {
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }

    time_diffs = df.index.to_series().diff()
    new_block  = (time_diffs > _SESSION_GAP) | time_diffs.isna()
    df["_block"] = new_block.cumsum()

    rs_kwargs: dict = {"closed": "right", "label": "right"}
    if _PD_GTE_1_1:
        rs_kwargs["offset"] = "45min"
    else:
        rs_kwargs["base"] = 45

    blocks: list[pd.DataFrame] = []
    for _, block_df in df.groupby("_block", sort=True):
        resampled = (
            block_df.drop(columns=["_block"])
            .resample(timeframe, **rs_kwargs)
            .agg(agg_rules)
        )
        resampled = resampled[resampled["Volume"] > 0].dropna(how="all")
        if not resampled.empty:
            blocks.append(resampled)

    if not blocks:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    df_out = pd.concat(blocks).sort_index()
    df_out.index.name = "Datetime"
    return df_out.reset_index()


def _resample_spot(df_1min: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Resample spot index 1-min OHLCV to *timeframe*.

    Uses standard bins (no 45-min offset) with block-based session isolation.
    Assumes data has already been filtered to spot session hours.
    """
    df = df_1min.copy()
    if "Datetime" in df.columns:
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    if df.empty:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    agg_rules = {
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }

    time_diffs = df.index.to_series().diff()
    new_block  = (time_diffs > _SESSION_GAP) | time_diffs.isna()
    df["_block"] = new_block.cumsum()

    # Standard bins for spot (no offset — 09:00, 09:05, 09:10 …)
    rs_kwargs: dict = {"closed": "right", "label": "right"}

    blocks: list[pd.DataFrame] = []
    for _, block_df in df.groupby("_block", sort=True):
        resampled = (
            block_df.drop(columns=["_block"])
            .resample(timeframe, **rs_kwargs)
            .agg(agg_rules)
        )
        resampled = resampled.dropna(how="all")
        if df["Volume"].sum() > 0:
            resampled = resampled[resampled["Volume"] > 0]
        if not resampled.empty:
            blocks.append(resampled)

    if not blocks:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    df_out = pd.concat(blocks).sort_index()
    df_out.index.name = "Datetime"
    return df_out.reset_index()


def _resample_60min(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Resample TXF 1-min OHLCV to 60-min using XQ's TXF-specific cut points.

    Day session: cut at :46 (bins labelled :45)
    Night session: cut at :01 (bins labelled :00)
    Uses composite key (session, label) to prevent cross-gap merges.
    """
    df = df_1min.copy()
    if "Datetime" in df.columns:
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    agg_rules = {
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }

    sessions = pd.Series(
        [_get_trading_session(ts.time()) for ts in df.index],
        index=df.index,
    )
    labels = df.index.map(_get_60k_label)

    valid    = sessions.notna() & labels.notna()
    df       = df[valid]
    sessions = sessions[valid]
    labels   = labels[valid]

    if df.empty:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    df = df.copy()
    df["_session"] = sessions.values
    df["_label"]   = labels.values

    df_out = (
        df.groupby(["_session", "_label"])
        .agg(agg_rules)
        .dropna(how="all")
    )
    df_out = df_out[df_out["Volume"] > 0]
    df_out = df_out.reset_index(level="_session", drop=True)
    df_out.index.name = "Datetime"
    return df_out.sort_index().reset_index()


def _resample_60min_spot(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    Resample spot index 1-min OHLCV to 60-min using spot-specific cut points.

    09:00 → grouped into 10:00 bar (first full hour)
    10:01–11:00 → 11:00, etc.
    13:01–13:30 → 13:30 (partial last bar)
    """
    df = df_1min.copy()
    if "Datetime" in df.columns:
        df = df.set_index("Datetime")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    agg_rules = {
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }

    labels = df.index.map(_get_60k_label_spot)
    valid  = labels.notna()
    df     = df[valid]
    labels = labels[valid]

    if df.empty:
        return pd.DataFrame(
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]
        )

    df = df.copy()
    df["_label"] = labels.values

    df_out = (
        df.groupby("_label")
        .agg(agg_rules)
        .dropna(how="all")
    )
    if df["Volume"].sum() > 0:
        df_out = df_out[df_out["Volume"] > 0]

    df_out.index.name = "Datetime"
    return df_out.sort_index().reset_index()


# ===========================================================================
# Public class
# ===========================================================================

class ShioajiDataFetcher:
    """
    Fetches K-line data from Shioaji for:
      - TXFR1   : 台指期貨近一連續合約
      - TSE/001 : 加權指數
      - OTC/101 : 櫃買指數

    The Shioaji API is a module-level singleton (login once at startup).
    1-min bars are fetched from the exchange and resampled locally.
    """

    def __init__(self, symbol: str = "TXFR1") -> None:
        self._symbol      = symbol
        self._is_spot     = symbol in _SPOT_SYMBOLS
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

        Parameters
        ----------
        timeframe : "1min", "5min", "10min", "15min", "30min", "60min"
        bars      : Maximum resampled bars to return (most recent N).
        start     : Date string "yyyy-mm-dd". Auto-derived if omitted.
        end       : Date string "yyyy-mm-dd". Defaults to today.

        Returns
        -------
        pd.DataFrame with columns: Datetime (tz-aware Asia/Taipei),
        Open, High, Low, Close, Volume.
        """
        supported = {"1min", "5min", "10min", "15min", "30min", "60min"}
        if timeframe not in supported:
            raise ValueError(
                f"Unsupported timeframe '{timeframe}'. Supported: {sorted(supported)}"
            )

        min_per_bar = int(timeframe.replace("min", "")) if timeframe != "1min" else 1
        needed_1min = int(bars * min_per_bar * 1.5)

        today = date.today()
        if end is None:
            end = today.strftime("%Y-%m-%d")
        if start is None:
            # Spot has shorter session → use a conservative per-day estimate
            mins_per_day  = 270 if self._is_spot else _TRADING_MIN_PER_DAY
            trading_days  = math.ceil(needed_1min / mins_per_day)
            lookback      = math.ceil(trading_days * 7 / 5) + 10
            start = (today - timedelta(days=lookback)).strftime("%Y-%m-%d")

        logger.info(
            "Fetching 1-min kbars: %s  %s → %s  (need ~%d 1-min bars for %d %s bars)",
            self._symbol, start, end, needed_1min, bars, timeframe,
        )

        # 最多允許一次自動重新登入（應對跨週末 Token 過期）
        _MAX_RELOGIN = 1
        kbars = None
        for _attempt in range(_MAX_RELOGIN + 1):
            api      = _get_api()
            contract = self._get_contract(api)
            try:
                kbars = api.kbars(contract, start=start, end=end)
                break   # 成功，跳出重試迴圈
            except Exception as exc:
                exc_str = str(exc).lower()

                # Token 過期 (401) → 自動重新登入後重試一次
                if _is_token_error(exc) and _attempt < _MAX_RELOGIN:
                    logger.warning(
                        "[%s] Token 過期 (401)，嘗試第 %d 次自動重新登入...",
                        self._symbol, _attempt + 1,
                    )
                    _force_relogin()
                    continue   # 回到迴圈頂端，使用新 api 重試

                # 資料權限不足
                if "permission" in exc_str or "unauthorized" in exc_str or "403" in exc_str:
                    logger.error(
                        "資料權限尚未開通！(%s)\n原始錯誤: %s", self._symbol, exc
                    )
                    raise PermissionError(
                        f"Shioaji 資料權限不足 ({self._symbol}): {exc}"
                    ) from exc

                # 其他錯誤
                logger.error("api.kbars() raised: %s", exc)
                raise RuntimeError(
                    f"Shioaji kbars fetch failed ({self._symbol}): {exc}"
                ) from exc

        # Normalise based on symbol type
        if self._is_spot:
            df_1min = _normalise_spot(kbars)
        else:
            df_1min = _normalise(kbars)

        if df_1min.empty:
            raise ValueError(
                f"No kbar data returned for {self._symbol} ({start} → {end})"
            )

        logger.info("Fetched %d 1-min bars from exchange (%s).", len(df_1min), self._symbol)

        # Resample
        if timeframe == "1min":
            df_out = df_1min
        elif timeframe == "60min":
            if self._is_spot:
                df_out = _resample_60min_spot(df_1min)
            else:
                df_out = _resample_60min(df_1min)
            logger.info(
                "60K resample (%s): %d bars (from %d 1-min bars)",
                self._symbol, len(df_out), len(df_1min),
            )
        else:
            if self._is_spot:
                df_out = _resample_spot(df_1min, timeframe)
            else:
                df_out = _resample_session_aware(df_1min, timeframe)
            logger.info(
                "Session-aware resample → %s (%s): %d bars (from %d 1-min bars)",
                timeframe, self._symbol, len(df_out), len(df_1min),
            )

        if df_out.empty:
            raise ValueError(
                f"Resample to {timeframe} produced no bars for {self._symbol}"
            )

        if len(df_out) > bars:
            df_out = df_out.iloc[-bars:].reset_index(drop=True)

        self._latest_close = float(df_out["Close"].iloc[-1])
        logger.info(
            "fetch_bars(%s, %s): returning %d bars | latest close = %.0f",
            self._symbol, timeframe, len(df_out), self._latest_close,
        )
        return df_out

    def get_latest_price(self) -> float:
        if self._latest_close is None:
            df = self.fetch_bars("5min", bars=2)
            return float(df["Close"].iloc[-1])
        return self._latest_close

    # ------------------------------------------------------------------
    # Contract lookup
    # ------------------------------------------------------------------

    def _get_contract(self, api: "sj.Shioaji"):
        if self._is_spot:
            return self._get_spot_contract(api)
        return self._get_futures_contract(api)

    def _get_spot_contract(self, api: "sj.Shioaji"):
        """Locate TSE/001 or OTC/101 index contract in Shioaji."""
        market, code = _SPOT_SYMBOLS[self._symbol]

        # Try multiple attribute names for different Shioaji versions
        indexs_group = None
        for attr in ("Indexs", "Index", "indexes"):
            indexs_group = getattr(api.Contracts, attr, None)
            if indexs_group is not None:
                break

        if indexs_group is None:
            raise RuntimeError(
                f"無法找到 Index 合約群組。\n"
                f"  請確認 Shioaji 版本並已成功 fetch_contracts()。\n"
                f"  Symbol: {self._symbol}"
            )

        market_group = getattr(indexs_group, market, None)
        if market_group is None:
            available = [a for a in dir(indexs_group) if not a.startswith("_")]
            raise RuntimeError(
                f"無法找到 {market} 在 Index 合約群組中。\n"
                f"  可用群組: {available}"
            )

        contract = getattr(market_group, code, None)
        if contract is None:
            available = [a for a in dir(market_group) if not a.startswith("_")]
            raise RuntimeError(
                f"無法找到合約 {code} 在 {market} 群組中。\n"
                f"  可用合約: {available}"
            )
        return contract

    @staticmethod
    def _get_futures_contract(api: "sj.Shioaji"):
        """Locate TXFR1 futures contract in Shioaji."""
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
    symbol:    str  = "TXFR1",
    timeframe: str  = "5min",
    bars:      int  = 200,
    start:     "str | None" = None,
    end:       "str | None" = None,
) -> pd.DataFrame:
    """Fetch K-line data for *symbol* at the given resolution."""
    return ShioajiDataFetcher(symbol).fetch_bars(
        timeframe=timeframe, bars=bars, start=start, end=end
    )
