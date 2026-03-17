"""
diagnose.py — 用來排查 Shioaji kbars 原始資料的問題。

執行方式：python diagnose.py
輸出重點：
  1. 原始 ts 值（nanoseconds）與轉換後的時間，確認是 bar 開始還是結束
  2. 每天的第一根與最後一根 bar，確認交易時間範圍
  3. 是否有非交易時間的 bar（盤前/盤後）
  4. 5K 重新取樣後的前幾根，供對照券商 App
"""

import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

import shioaji as sj  # noqa: E402

# ── 登入 ──────────────────────────────────────────────────────────────────────
api = sj.Shioaji()
api.login(
    api_key=os.environ["SHIOAJI_API_KEY"],
    secret_key=os.environ["SHIOAJI_SECRET_KEY"],
)

contract = api.Contracts.Stocks.TSE["2330"]

# 只抓最近 3 個交易日，方便對照
end   = date.today().strftime("%Y-%m-%d")
start = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
print(f"\n抓取範圍：{start} → {end}\n")

kbars = api.kbars(contract, start=start, end=end)
df_raw = pd.DataFrame({**kbars})

# ── 1. 顯示原始 ts 欄位的前 5 筆，確認 unit ────────────────────────────────
print("=" * 60)
print("【原始 ts 值（前 5 筆）】")
print(df_raw[["ts"]].head())
print(f"\nts dtype     : {df_raw['ts'].dtype}")
print(f"ts 最小值    : {df_raw['ts'].min()}")
print(f"ts 最大值    : {df_raw['ts'].max()}")

# ── 2. 轉換成台灣時間 ─────────────────────────────────────────────────────────
df_raw["dt"] = pd.to_datetime(df_raw["ts"], unit="ns", utc=True).dt.tz_convert("Asia/Taipei")

print("\n【轉換後 datetime（前 5 筆）】")
print(df_raw[["ts", "dt", "Open", "High", "Low", "Close", "Volume"]].head().to_string())

# ── 3. 每天的第一根與最後一根 ─────────────────────────────────────────────────
print("\n" + "=" * 60)
print("【每日第一根 & 最後一根 bar】")
df_raw["date"] = df_raw["dt"].dt.date
for d, grp in df_raw.groupby("date"):
    first = grp.iloc[0]
    last  = grp.iloc[-1]
    print(
        f"  {d}  "
        f"第一根: {first['dt'].strftime('%H:%M:%S')}  O={first['Open']}  "
        f"→  最後一根: {last['dt'].strftime('%H:%M:%S')}  C={last['Close']}  "
        f"共 {len(grp)} 根"
    )

# ── 4. 非交易時間 bar（09:00 前 或 13:30 後）────────────────────────────────
print("\n" + "=" * 60)
print("【非交易時間 bar（09:00 前 或 13:30 後）】")
t = df_raw["dt"].dt.time
import datetime
non_trading = df_raw[
    (t < datetime.time(9, 0)) | (t > datetime.time(13, 30))
]
if non_trading.empty:
    print("  ✓ 無非交易時間資料")
else:
    print(non_trading[["dt", "Open", "High", "Low", "Close", "Volume"]].to_string())

# ── 5. 5K 重新取樣後前 10 根，供對照券商 App ──────────────────────────────────
print("\n" + "=" * 60)
print("【5K 重新取樣（最近 10 根）—— 請對照券商 App 確認 OHLCV 是否吻合】")
df_clean = df_raw.set_index("dt")[["Open", "High", "Low", "Close", "Volume"]].sort_index()

df_5k = (
    df_clean.resample("5min", label="left", closed="left")
    .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
    .dropna()
)
print(df_5k.tail(10).to_string())

# ── 結束 ──────────────────────────────────────────────────────────────────────
api.logout()
print("\n診斷完成。")
