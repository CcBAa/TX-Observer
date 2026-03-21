# TX-Observer

**台灣期貨 + 現貨 K 線自動推播系統**
*Automated Taiwan Futures & Spot Index K-line Chart Push Notification System*

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Linux%20Headless-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)
![API](https://img.shields.io/badge/Data-Shioaji%20API-orange)
![Push](https://img.shields.io/badge/Push-LINE%20Messaging%20API-brightgreen)

---

## 專案簡介

TX-Observer 是一套部署於無頭 Linux 伺服器的**全自動台股技術分析推播機器人**。

系統依據嚴格的交易時段排程，自動完成以下流程：

```
Shioaji API 抓取 1-min 原始 K 棒
        ↓
本地 Resample → 5分K + 60分K
        ↓
mplfinance 畫圖（含 MA5/10/20/60/240、十字線標記）
        ↓
Pillow 垂直拼合 → 單一 PNG（5K 上 / 60K 下）
        ↓
上傳 Imgbb 圖床取得公開 URL
        ↓
LINE Messaging API Push（文字摘要 + 圖片，1 計費單位）
        ↓
刪除本地暫存 PNG
```

> **設計原則**：全程無 GUI（Matplotlib Agg backend）、憑證僅從 `.env` 讀取、休市日完全不呼叫任何 API、台指期結算日自動提醒。

---

## 監控品種

| 代碼 | 中文名稱 | 類型 | 交易時段 |
|------|---------|------|---------|
| `TXFR1` | 台指期近一連續合約 | 期貨 | 日盤 08:45–13:45 + 夜盤 15:00–隔日 05:00 |
| `TSE/001` | 加權指數 | 現貨 | 週一~五 09:00–13:30 |
| `OTC/101` | 櫃買指數 | 現貨 | 週一~五 09:00–13:30 |

---

## 功能特色

### 1. 自動 K 線圖（雙週期合圖）

每次觸發輸出一張垂直合圖：

```
┌─────────────────────────────────────┐
│  [台指期近一]  5K                    │  ← 上圖（60% 高度）
│  蠟燭圖 + 成交量 + MA5/10/20/60/240  │
└─────────────────────────────────────┘
┌─────────────────────────────────────┐
│  [台指期近一]  60K                   │  ← 下圖（40% 高度）
│  蠟燭圖 + 成交量 + MA5/10/20/60/240  │
└─────────────────────────────────────┘
```

**圖表規格：**

| 項目 | 說明 |
|------|------|
| 主題 | 暗色系（`nightclouds` 基底，背景 `#0d1117`） |
| 漲跌色 | **紅漲綠跌**（台灣股市慣例） |
| 十字線 | 開收盤同價的 Doji K 棒自動標金色 |
| 中文字體 | 思源黑體繁體中文（`NotoSansCJKtc`）自動偵測或下載 |
| 解析度 | 150 DPI |
| MA 線 | MA5（金）/ MA10（藍）/ MA20（粉）/ MA60（橙）/ MA240（白） |
| 顯示窗口 | 5K：最新 120 根；60K：最新 80 根 |
| MA 計算 | 基於**完整資料集**計算後再截取顯示窗口，邊緣值準確 |

### 2. 智慧排程與交易時段過濾

**雙層防護機制：**

- **第一層 — 行事曆層**：每天 08:30 透過 `pandas_market_calendars` XTAI 行事曆判斷今天是否開盤
  - 正確處理國定假日、農曆年、颱風停市、**週六補班開盤**
  - 休市日所有任務直接跳過，完全不呼叫 Shioaji API 或 LINE

- **第二層 — 時段層**：每個任務觸發時再次確認當前時間是否在交易時段內

**完整排程時間表（Asia/Taipei）：**

| 任務 | 觸發日 | 觸發時間 |
|------|--------|---------|
| 行事曆檢查 | 週一~六 | 08:30 |
| 台指期（日盤） | 週一~五 | 08:45 · 09:45 · 10:45 · 11:45 · 12:45 · 13:45 |
| 台指期（夜盤前段） | 週一~五 | 15:00 · 16:00 · … · 23:00（每整點）|
| 台指期（夜盤後段） | 週二~六 | 00:00 · 01:00 · … · 05:00（每整點）|
| 加權 + 櫃買 | 週一~五 | 09:00 · 10:00 · 11:00 · 12:00 · 13:00 |

### 3. 台指期結算日自動提醒

- 判斷邏輯：**每月第三個週三**（日期落在 15~21 日之間的週三）
- 結算日當天台指期 LINE 訊息自動加上首行：`【今日台指結算日】`
- 僅影響 TXFR1；加權、櫃買訊息不受影響，仍正常推送

**LINE 訊息格式（結算日範例）：**

```
【今日台指結算日】
[TX-Observer]  2026-03-18 09:45 (UTC+8)
台指期近一 (TXFR1)
Last:        20,123
Change:  ▲ 45  (+0.22%)
Charts:  5K + 60K 合圖
```

### 4. 進階 Resample 邏輯

| 品種 | 5K Resample | 60K Resample |
|------|-------------|--------------|
| TXFR1 | `offset='45min'`（XQ 標準，以 08:45 為基準） | 日盤切點 `:46`，夜盤切點 `:01` |
| TSE/OTC | 標準整點區間 | 切點 `:01`（首根 09:00 歸入 10:00 棒） |

Session 隔離：偵測超過 70 分鐘的時間空白自動切分 session，防止日盤與夜盤的 K 棒跨 session 合併。

### 5. 錯誤隔離

單一品種失敗（抓取、繪圖、上傳、推送任一環節）只記錄 Error log，不影響其他品種繼續執行，排程器持續運作不崩潰。

### 6. CLI 測試工具

```bash
# 立即執行全部品種（忽略交易時段，用於部署後驗證）
python main.py --run-now

# 只測特定品種
python main.py --run-now --symbol TXFR1
python main.py --run-now --symbol TSE/001
python main.py --run-now --symbol OTC/101
```

---

## 系統架構

```
TX-Observer/
├── main.py          # 主程式：排程控制、市場行事曆、結算日提醒、任務編排
├── config.py        # 配置模組：載入 .env、憑證驗證、Logging 初始化
├── fetcher.py       # 資料模組：Shioaji API 單例、Resample、Session 隔離
├── renderer.py      # 繪圖模組：mplfinance 暗色 K 線圖、Pillow 合圖、CJK 字體
├── notifier.py      # 推播模組：Imgbb 上傳 + LINE Messaging API Push
├── diagnose.py      # 診斷工具：環境、API 連線、字體等自我檢測
├── fonts/           # 自動建立：CJK 字體快取目錄（.gitignore 排除）
├── charts/          # 自動建立：暫存 PNG 圖片（推送後即刪除，.gitignore 排除）
├── .env             # 私密憑證（.gitignore 排除，絕不 commit）
├── .env.example     # 憑證範本（可安全 commit）
├── .gitignore       # Git 排除規則
├── requirements.txt # Python 套件清單
└── README.md        # 本文件
```

---

## 事前準備

| 項目 | 需求 |
|------|------|
| Python | 3.10 或以上 |
| 作業系統 | Linux（推薦 Oracle Cloud Ubuntu 22.04）|
| 永豐金券商帳戶 | 已申請並開通 Shioaji API 權限 |
| LINE Developers 帳號 | 已建立 Messaging API Channel |
| Imgbb 帳號 | 免費申請，取得 API Key |

---

## 安裝步驟

### Step 1 — 複製專案

```bash
git clone https://github.com/<your-username>/TX-Observer.git
cd TX-Observer
```

### Step 2 — 建立虛擬環境

```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3 — 安裝依賴套件

```bash
pip install -r requirements.txt
```

### Step 4 — 安裝系統字體（Ubuntu 伺服器）

確保中文標題能正確顯示，無需手動下載字體：

```bash
sudo apt-get install -y fonts-noto-cjk
```

> 若未安裝，程式會嘗試從系統快取自動尋找，找不到時 log 會提示安裝指令。

### Step 5 — 設定環境變數

```bash
cp .env.example .env
nano .env   # 填入下方三組金鑰
```

編輯 `.env`：

```dotenv
SHIOAJI_API_KEY=<永豐金 API Key>
SHIOAJI_SECRET_KEY=<永豐金 Secret Key>
LINE_CHANNEL_ACCESS_TOKEN=<LINE Channel Access Token>
LINE_TARGET_ID=<LINE 群組 ID 或個人 ID>
IMGBB_API_KEY=<Imgbb API Key>
```

### Step 6 — 驗證安裝

```bash
python main.py --run-now
```

成功時 log 應依序出現：
```
TX-Observer Starting
All credentials loaded successfully.
Shioaji API ready.
今日台股開盤，排程任務正常執行。
[台指期近一] triggered at ...
[台指期近一] Job completed successfully.
...
```

### Step 7 — 啟動排程器

```bash
python main.py
```

---

## 取得各服務金鑰

### Shioaji API（永豐金）

1. 至 [永豐金 e-Leader](https://www.sinotrade.com.tw/) 開戶並申請 API 使用權限
2. 登入後前往「API 管理」→「建立金鑰」
3. 取得 `API_KEY` 與 `SECRET_KEY`
4. 詳細文件：https://sinotrade.github.io/

### LINE Channel Access Token

1. 前往 [LINE Developers Console](https://developers.line.biz/)
2. 建立 Provider → 建立 Messaging API Channel
3. 進入 Channel → **Basic settings** → **Channel access token** → Issue
4. 複製長效型 Token（約 172 字元）

### LINE_TARGET_ID（群組或個人 ID）

1. 在 Channel 設定中啟用 **Webhook**
2. 將 Bot 加入目標 LINE 群組（或加好友）
3. 對 Bot 發送任意一則訊息
4. 從 Webhook 事件 payload 中取得：
   - 群組推播：`source.groupId`（開頭為 `C`，共 33 字元）
   - 個人推播：`source.userId`（開頭為 `U`，共 33 字元）

### Imgbb API Key

1. 前往 [https://imgbb.com/](https://imgbb.com/) 免費註冊
2. 右上角頭像 → **About** → **API** → 申請 API Key

---

## 背景執行（Headless 伺服器）

### 方式一：nohup（快速）

```bash
nohup python main.py >> app.log 2>&1 &
echo $! > tx_observer.pid   # 記錄 PID 供之後停止用
```

停止：
```bash
kill $(cat tx_observer.pid)
```

### 方式二：systemd（推薦，開機自啟）

建立 `/etc/systemd/system/tx-observer.service`：

```ini
[Unit]
Description=TX-Observer — Taiwan Futures & Spot Chart Push Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/TX-Observer
ExecStart=/home/ubuntu/TX-Observer/venv/bin/python main.py
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

啟用服務：
```bash
sudo systemctl daemon-reload
sudo systemctl enable tx-observer
sudo systemctl start tx-observer
```

查看即時 log：
```bash
sudo journalctl -u tx-observer -f
```

---

## 日誌說明

所有 INFO / WARNING / ERROR 日誌同時輸出至：

| 輸出位置 | 說明 |
|---------|------|
| **stdout** | systemd journal 即時監看 |
| **`app.log`** | 本地持久化存檔（已加入 `.gitignore`）|

**常見 log 訊息說明：**

| Log 訊息 | 含義 |
|---------|------|
| `今日台股開盤，排程任務正常執行。` | 當天為正常交易日 |
| `今日為台股休市日，系統進入休眠模式。` | 假日或補假，所有任務跳過 |
| `[台指期近一] 今日為台指期結算日。` | 每月第三個週三 |
| `Futures outside trading hours — skipped.` | 觸發時間不在交易時段 |
| `[XXX] Job completed successfully.` | 完整流程正常結束 |
| `CJK font found at static path: ...` | 中文字體載入成功 |

---

## 資安說明

| 保護項目 | 措施 |
|---------|------|
| 所有 API 金鑰 | 僅從 `.env` 讀取，程式碼中無任何硬編碼 |
| `.env` 檔案 | 已加入 `.gitignore`，不會被 git 追蹤 |
| 圖表 PNG | 推送後立即刪除，`charts/` 已加入 `.gitignore` |
| Log 檔案 | `*.log` 已加入 `.gitignore` |
| `.env.example` | 僅含欄位名稱與說明，**不含任何真實金鑰**，可安全公開 |

> **警告**：請確認 `.env` 從未出現在任何 `git commit` 記錄中。
> 如有疑慮，可執行 `git log --all --full-history -- .env` 確認。

---

## 常見問題

**Q：首次執行出現 `Glyph missing from current font` 警告**

```bash
sudo apt-get install -y fonts-noto-cjk
```

**Q：`IndexError: list assignment index out of range`（Shioaji 內部）**

此為 `pysolace` 函式庫的非致命內部錯誤，不影響程式運作。
可選擇安裝 speed 套件減少此警告：
```bash
pip install shioaji[speed]
```

**Q：`pandas_market_calendars not installed — holiday detection disabled`**

```bash
pip install pandas-market-calendars
```

**Q：LINE Push 回應 `403` 或 `401`**

- 確認 `LINE_CHANNEL_ACCESS_TOKEN` 未過期（長效型 Token 無到期日）
- 確認 Bot 仍在目標群組中
- 確認 `LINE_TARGET_ID` 格式正確（群組以 `C` 開頭）

**Q：要如何停止排程器？**

前景執行：`Ctrl + C`
systemd：`sudo systemctl stop tx-observer`
nohup：`kill $(cat tx_observer.pid)`

---

## 依賴套件

| 套件 | 用途 |
|------|------|
| `shioaji` | 永豐金 Shioaji API（台股資料來源） |
| `pandas-market-calendars` | XTAI 台灣行事曆（假日偵測） |
| `pandas` / `numpy` | 資料處理與 Resample |
| `mplfinance` | K 線圖繪製 |
| `matplotlib` | 圖形後端（Agg，無 GUI） |
| `Pillow` | 雙圖拼合 |
| `requests` | Imgbb 上傳 + LINE Push |
| `APScheduler` | 多 Cron 排程器 |
| `pytz` | Asia/Taipei 時區處理 |
| `python-dotenv` | `.env` 環境變數載入 |

---

## License

MIT License © 2026

---

## Contributing

歡迎提交 Issue 或 Pull Request。

新增品種請在 `main.py` 的 `_FUTURES_SYMBOLS` 或 `_SPOT_SYMBOLS` 新增元組，
`fetcher.py` 的 `_SPOT_SYMBOLS` dict 對應 Shioaji 合約代碼。
