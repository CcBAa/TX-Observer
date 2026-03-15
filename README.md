# TX-Observer

**台指期貨 K 線圖自動截圖與 LINE 推播系統**
*Automated Taiwan Futures (TX) K-line Chart Screenshot & LINE Push Notification System*

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Headless-lightgrey)

---

## 專案簡介 | Overview

TX-Observer 是一套部署於無頭 Linux 伺服器（Headless Server）的量化輔助工具，依據嚴格降頻排程自動完成以下流程：

1. 抓取台指期近月（TX）模擬報價（MockDataFetcher，可替換為真實券商 API）
2. 將 1 分鐘 K 線 Resample 為 **5 分鐘（5K）** 與 **60 分鐘（60K）** 週期
3. 使用 `mplfinance` 在背景繪製帶均線（MA5/10/20）及成交量副圖的暗色系 K 線圖
4. 將本地 PNG 上傳至 **Imgbb** 圖床，取得公開 HTTPS URL
5. 透過 **LINE Messaging API Push** 將文字行情摘要與兩張圖表 URL 組合成 **1 則 API 請求**發送
6. 推播完成後自動刪除本地暫存圖片

> **設計原則**：所有繪圖操作強制使用 `matplotlib Agg` 後端，完全不依賴圖形介面；排程每月約 152 次推播，嚴格控制在 LINE Messaging API 200 則免費額度以內。

---

## 系統架構 | Architecture

```
TX-Observer/
├── main.py          # 主程式：排程控制、交易時段過濾、任務編排
├── config.py        # 配置模組：載入 .env、驗證憑證、初始化 Logging
├── fetcher.py       # 資料模組：MockDataFetcher、Resample 5K/60K
├── renderer.py      # 繪圖模組：mplfinance 暗色 K 線圖（Headless）
├── notifier.py      # 推播模組：Imgbb 圖床上傳 + LINE Messaging API Push
├── .env.example     # 環境變數範本
├── .gitignore       # Git 排除規則（.env、圖片、Log 均已排除）
├── requirements.txt # Python 套件清單
└── README.md        # 本文件
```

### 資料流程圖

```
MockDataFetcher
      │  1-min OHLCV bars
      ▼
  resample_to_timeframe()
      │  df_5k / df_60k
      ▼
  render_chart()  ──►  charts/tx_60k_*.png
                  ──►  charts/tx_5k_*.png
                              │
                              ▼
                    upload_to_imgbb()  ──►  https://i.ibb.co/…
                              │
                              ▼
                    send_push_message()  ──►  LINE group / user
                              │
                              ▼
                    unlink() local PNGs
```

---

## 排程設計 | Scheduler Design

使用 **兩個 APScheduler CronTrigger**（時區：Asia/Taipei）：

| Trigger | 觸發日 | 觸發時間 |
|---------|--------|---------|
| A — 盤中 | 週一至週五 | 09:00 · 11:00 · 13:00 · 15:00 · 21:00 · 23:00 |
| B — 夜盤收盤 | 週二至週六 | 05:00 |

**月均推播次數：**
- 週一：6 次；週二–五：各 7 次；週六：1 次
- 每週 **35 次** × 4.33 週 ≈ **每月 152 次**（< 200 則免費上限）

週末休市期間（週六 05:00 後至週一 08:45 前）無任何 Trigger，額外設有交易時段安全門（`is_trading_time()`）作為第二道防線。

---

## 交易時段邏輯 | Trading Hours

| 時段 | 時間（UTC+8） |
|------|--------------|
| 日盤 | 週一至週五  08:45 – 13:45 |
| 夜盤 | 週一至週五  15:00 – 隔日 05:00 |
| 週六 | 00:00 – 05:00（週五夜盤延續） |
| 週日 | 全日休市 |
| 週一凌晨 | 00:00 – 08:44 休市（無週日夜盤） |

---

## 事前準備 | Prerequisites

| 項目 | 需求 |
|------|------|
| Python | 3.9 或以上 |
| 作業系統 | Linux（推薦 Oracle Cloud Linux 9）或 macOS |
| LINE Messaging API Channel | LINE Developers Console 建立 |
| Imgbb API Key | https://api.imgbb.com 免費申請 |

---

## 安裝步驟 | Installation

```bash
# 1. 複製專案
git clone https://github.com/<your-username>/TX-Observer.git
cd TX-Observer

# 2. 建立並啟用虛擬環境
python3 -m venv venv
source venv/bin/activate

# 3. 安裝依賴套件
pip install -r requirements.txt

# 4. 設定環境變數
cp .env.example .env
# 用文字編輯器開啟 .env，填入三個必要金鑰
```

---

## 設定說明 | Configuration

編輯 `.env`（已被 `.gitignore` 排除，不會提交）：

```dotenv
# LINE Messaging API — Channel Access Token (long-lived)
LINE_CHANNEL_ACCESS_TOKEN=your_channel_access_token_here

# 目標 Group ID (C…) 或 User ID (U…)
LINE_TARGET_ID=your_group_or_user_id_here

# Imgbb 圖床 API Key
IMGBB_API_KEY=your_imgbb_api_key_here
```

完整可用變數請參考 `.env.example`。

---

## 如何取得 LINE_TARGET_ID

1. 在 LINE Developers Console 啟用 **Webhook**。
2. 將 Bot 加入目標群組或與目標用戶加好友。
3. 對 Bot 發送任意訊息，Webhook 事件中 `source.groupId`（群組）或 `source.userId`（個人）即為 `LINE_TARGET_ID`。

---

## 啟動方式 | Usage

### 立即測試一次（推薦初次使用）

```bash
python main.py --run-now
```

此指令會繞過交易時段過濾，立刻執行完整的「抓取→渲染→上傳→推播→清理」流程，確認各服務金鑰正確後再啟動排程器。

### 啟動排程模式

```bash
python main.py
```

程式持續在前景執行，依排程自動觸發任務。

### 背景執行（Headless 伺服器）

```bash
# nohup 簡易方案
nohup python main.py > /dev/null 2>&1 &

# 建議使用 systemd（見下方範例）
```

### systemd 服務設定

建立 `/etc/systemd/system/tx-observer.service`：

```ini
[Unit]
Description=TX-Observer Futures Chart Push Service
After=network.target

[Service]
Type=simple
User=your_linux_user
WorkingDirectory=/path/to/TX-Observer
ExecStart=/path/to/TX-Observer/venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable tx-observer
sudo systemctl start tx-observer
sudo systemctl status tx-observer
```

---

## 日誌 | Logging

所有 INFO / ERROR 日誌同時輸出至：

- **stdout**：方便 `systemd journal` 即時監看（`journalctl -u tx-observer -f`）
- **`app.log`**：本地持久化存檔，供無頭伺服器除錯

> `app.log` 已加入 `.gitignore`，不會提交至版本控制。

---

## 替換真實 API | Replacing MockDataFetcher

`fetcher.py` 的 `MockDataFetcher` 遵循以下介面約定：

```python
def fetch_1min_bars(self, periods: int) -> pd.DataFrame:
    # 必須回傳欄位：Datetime, Open, High, Low, Close, Volume
    ...
```

只需實作符合相同簽名的類別（例如 `FugleDataFetcher`），在 `main.py` 中替換 `MockDataFetcher()` 即可，其餘模組無需修改。

---

## 圖表樣式 | Chart Style

| 項目 | 說明 |
|------|------|
| 主題 | 暗色系（`nightclouds` 基底） |
| 漲跌色 | 紅漲綠跌（台灣股市慣例） |
| 均線 | MA5（金）/ MA10（藍）/ MA20（粉） |
| 副圖 | 成交量（Volume），同步漲跌色 |
| 解析度 | 150 DPI，18 × 10 英吋 |
| 輸出格式 | PNG，推播後即刪除本地檔案 |

---

## 資安說明 | Security

- 所有憑證（Token、API Key）僅透過 `.env` 讀取，絕不 Hardcode。
- `.env`、`*.log`、`*.png`、`charts/` 均已加入 `.gitignore`。
- `.env.example` 僅含 Key 名稱，Value 留空，可安全提交。

---

## 授權 | License

MIT License

---

## 貢獻 | Contributing

歡迎提交 Issue 或 Pull Request。
若要整合真實券商 API，請在 `fetcher.py` 建立新的 Fetcher 類別並確保回傳格式與 `MockDataFetcher` 一致。
