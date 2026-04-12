# TX-Observer

**台灣期貨 + 現貨 K 線自動推播系統**
*Automated Taiwan Futures & Spot Index K-line Chart Push Notification System*

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Linux%20Headless-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)
![API](https://img.shields.io/badge/Data-Shioaji%20API-orange)
![Push](https://img.shields.io/badge/Push-Discord%20%2B%20Telegram-blueviolet)

---

## 專案簡介

TX-Observer 是一套部署於無頭 Linux 伺服器的**全自動台股技術分析推播機器人**。

系統依據嚴格的交易時段排程，自動完成以下流程：

```
Shioaji API 抓取 1-min 原始 K 棒
        ↓
本地 Resample → 5分K / 60分K / 日K
        ↓
matplotlib GridSpec 單一 Figure（含 MA5/10/20/60/240、十字線標記）
        ↓
匯出單一 PNG（期貨：60K 上 60% / 5K 下 40%；現貨：日K 上 56% / 60K 下 44%）
        ↓
直接上傳至 Discord Webhook（multipart binary）
        ↓
直接上傳至 Telegram Bot（sendPhoto binary）
        ↓
刪除本地暫存 PNG
```

> **設計原則**：全程無 GUI（Matplotlib Agg backend）、憑證僅從 `.env` 讀取、休市日完全不呼叫任何 API、台指期結算日自動提醒、不依賴外部圖床。

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

每次觸發輸出一張單一 Figure 合圖（無需 Pillow 拼合）：

**期貨模式（TXFR1）：**

```
┌─────────────────────────────────────┐
│  [台指期近一]  60K                   │  ← 上圖（~60% 高度）
│  蠟燭圖 + MA5/10/20/60/240           │
├──── MA5 ── MA10 ── MA20 ── MA60 ── MA240 ────┤  ← 共享 Legend
│  [台指期近一]  5K                    │  ← 下圖（~40% 高度）
│  蠟燭圖 + MA5/10/20/60/240           │
└─────────────────────────────────────┘
```

**現貨模式（TSE/OTC）：**

```
┌─────────────────────────────────────┐
│  [加權指數]  日K                     │  ← 上圖（~56% 高度）
│  蠟燭圖 + MA5/10/20/60/240           │
├──── MA5 ── MA10 ── MA20 ── MA60 ── MA240 ────┤  ← 共享 Legend
│  [加權指數]  60K                     │  ← 下圖（~44% 高度）
│  蠟燭圖 + MA5/10/20/60/240           │
└─────────────────────────────────────┘
```

**圖表規格：**

| 項目 | 說明 |
|------|------|
| 主題 | 暗色系（`nightclouds` 基底，背景 `#0d1117`） |
| 漲跌色 | **紅漲綠跌**（台灣股市慣例） |
| 十字線 | 開收盤同價的 Doji K 棒自動標金色 |
| 中文字體 | `NotoSansTC-Regular.ttf`，自動偵測系統字體或快取 |
| 解析度 | 300 DPI |
| MA 線 | MA5（金）/ MA10（藍）/ MA20（粉）/ MA60（橙）/ MA240（白） |
| 共享 Legend | 單一橫向 Legend 列置於兩張子圖之間，不重複顯示 |
| 顯示窗口 | 5K：最新 90 根；60K：最新 65 根；日K：最新 45 根 |
| 最高/最低標注 | 顯示窗口內最高 High（▲ 紅）與最低 Low（▼ 綠）自動標價 |
| MA 計算 | 基於**完整資料集**計算後再截取顯示窗口，邊緣值準確 |
| 渲染架構 | 單一 `matplotlib Figure` + `GridSpec`；mplfinance 外部 axes 模式，無需 Pillow 拼合 |

### 2. 智慧排程與交易時段過濾

**雙層防護機制：**

- **第一層 — 行事曆層**：每天 08:30 透過 `pandas_market_calendars` XTAI 行事曆判斷今天是否開盤
  - 正確處理國定假日、農曆年、颱風停市、**週六補班開盤**
  - 休市日所有任務直接跳過，完全不呼叫 Shioaji API 或推播

- **第二層 — 時段層**：每個任務觸發時再次確認當前時間是否在交易時段內

**完整排程時間表（Asia/Taipei）：**

| 任務 | 觸發日 | 觸發時間 |
|------|--------|---------|
| 行事曆檢查 | 週一~六 | 08:30 |
| 台指期（日盤） | 週一~五 | 08:45 · 09:45 · 10:45 · 11:45 · 12:45 |
| 台指期（日盤收盤，retry×3） | 週一~五 | **13:45:10**（`【今日收盤總結】`） |
| 台指期（夜盤前段） | 週一~五 | 15:00 · 16:00 · … · 23:00（每整點）|
| 台指期（夜盤後段） | 週二~六 | 00:00 · 01:00 · … · 04:00（每整點）|
| 台指期（夜盤收盤，retry×3） | 週二~六 | **05:00:10**（`【今日收盤總結】`） |
| 加權 + 櫃買（盤中） | 週一~五 | 09:00 · 10:00 · 11:00 · 12:00 · 13:00 |
| 加權 + 櫃買（收盤，retry×3） | 週一~五 | **13:30:10**（`【今日收盤總結】`） |

> **收盤補送設計**：收盤 job 延遲 10 秒觸發（確保交易所完成最後一根 K 棒），並內建最多 3 次重試（間隔 5 秒）以應對 API 資料延遲。

### 3. 台指期結算日自動提醒

- 判斷邏輯：**每月第三個週三**（日期落在 15~21 日之間的週三）
- 結算日當天台指期訊息自動加上首行：`【今日台指結算日】`
- 僅影響 TXFR1；加權、櫃買訊息不受影響，仍正常推送

**訊息格式（結算日範例）：**

```
【今日台指結算日】
[TX-Observer]  2026-03-18 09:45 (UTC+8)
台指期近一 (TXFR1)
Last:        20,123
Change:  ▲ 45  (+0.22%)
Charts:  60K + 5K 合圖
```

### 4. 進階 Resample 邏輯

| 品種 | 5K Resample | 60K Resample | 日K Resample |
|------|-------------|--------------|--------------|
| TXFR1 | `offset='45min'`（XQ 標準，以 08:45 為基準） | 日盤切點 `:46`，夜盤切點 `:01` | — |
| TSE/OTC | 標準整點區間 | 切點 `:01`（首根 09:00 歸入 10:00 棒） | 09:00–13:30 日內匯總 |

Session 隔離：偵測超過 70 分鐘的時間空白自動切分 session，防止日盤與夜盤的 K 棒跨 session 合併。

### 5. 錯誤隔離

單一品種失敗（抓取、繪圖、上傳、推送任一環節）只記錄 Error log，不影響其他品種繼續執行，排程器持續運作不崩潰。Discord 與 Telegram 推播彼此獨立隔離，一方失敗不影響另一方。

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
├── fetcher.py       # 資料模組：Shioaji API 單例、Resample、Session 隔離、MA 預計算
├── renderer.py      # 繪圖模組：mplfinance 暗色 K 線圖、GridSpec 單一 Figure、CJK 字體
├── notifier.py      # 推播模組：Discord Webhook + Telegram Bot 雙平台推送
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
| Discord 伺服器 | 已建立三個頻道（TX / TSE / OTC）並取得各 Webhook URL |
| Telegram Bot | 已建立 Bot 並加入超級群組，設定三個論壇主題（Topics） |

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

確保中文標題能正確顯示：

```bash
sudo apt-get install -y fonts-noto-cjk
```

> 若未安裝，程式會嘗試從系統快取自動尋找，找不到時 log 會提示安裝指令。

### Step 5 — 設定環境變數

```bash
cp .env.example .env
nano .env   # 填入下方各組金鑰
```

編輯 `.env`：

```dotenv
# Shioaji API
SHIOAJI_API_KEY=<永豐金 API Key>
SHIOAJI_SECRET_KEY=<永豐金 Secret Key>

# Discord Webhooks（各品種獨立頻道）
DISCORD_WEBHOOK_TX=<台指期 Discord Webhook URL>
DISCORD_WEBHOOK_TSE=<加權指數 Discord Webhook URL>
DISCORD_WEBHOOK_OTC=<櫃買指數 Discord Webhook URL>

# Telegram Bot
TELEGRAM_BOT_TOKEN=<Bot Token>
TELEGRAM_CHAT_ID=<超級群組 Chat ID（負數）>
TELEGRAM_THREAD_TX=<台指期論壇主題 ID>
TELEGRAM_THREAD_TSE=<加權指數論壇主題 ID>
TELEGRAM_THREAD_OTC=<櫃買指數論壇主題 ID>
```

### Step 6 — 驗證安裝

```bash
python main.py --run-now
```

成功時 log 應依序出現：

```
TX-Observer Starting
All credentials loaded successfully.
Shioaji API 登入成功。
合約驗證通過：TXFR1 / TSE001 / OTC101 均已確認。
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

### Discord Webhook URL

1. 在 Discord 伺服器中，對目標頻道點右鍵 → **「編輯頻道」**
2. 進入「整合」→「Webhook」→「建立 Webhook」
3. 複製 Webhook URL（格式：`https://discord.com/api/webhooks/...`）
4. 為 TX / TSE / OTC 三個品種各建立一個 Webhook（可同頻道或不同頻道）

### Telegram Bot Token 與 Chat ID

1. 在 Telegram 搜尋 **@BotFather**，傳送 `/newbot` 建立 Bot
2. 複製 Token（格式：`123456789:ABC-DEF...`）
3. 將 Bot 加入目標**超級群組**並賦予發送訊息權限
4. 呼叫 `https://api.telegram.org/bot<TOKEN>/getUpdates`，從回應取得 `chat.id`（負數）

### Telegram 論壇主題 ID（TELEGRAM_THREAD_*）

1. 在超級群組開啟「Topics」（論壇功能）並建立三個主題（TX / TSE / OTC）
2. 傳送測試訊息後，呼叫 `getUpdates`
3. 從回應的 `message_thread_id` 欄位取得各主題 ID

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
| `Discord upload OK (HTTP 200).` | Discord 推播成功 |
| `Telegram upload OK (HTTP 200).` | Telegram 推播成功 |
| `CJK font loaded from cache: ...` | 中文字體載入成功 |

---

## 資安說明

| 保護項目 | 措施 |
|---------|------|
| 所有 API 金鑰 / Token / Webhook URL | 僅從 `.env` 讀取，程式碼中無任何硬編碼 |
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

**Q：Discord 回應 `401` 或 `404`**

- 確認 Webhook URL 正確且未被刪除（Discord 伺服器管理者可重新產生）
- 確認 `.env` 中的 `DISCORD_WEBHOOK_TX/TSE/OTC` 對應品種正確

**Q：Telegram 回應 `400 Bad Request`**

- 確認 `TELEGRAM_CHAT_ID` 為超級群組 ID（負數，格式 `-100xxxxxxxxxx`）
- 確認 `TELEGRAM_THREAD_*` 為正確的論壇主題 ID
- 確認 Bot 已加入群組且具備發送訊息權限

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
| `mplfinance` | K 線圖繪製（外部 axes 模式） |
| `matplotlib` | 圖形後端（Agg，無 GUI）+ GridSpec 版面 |
| `requests` | Discord Webhook 上傳 + Telegram Bot API |
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
