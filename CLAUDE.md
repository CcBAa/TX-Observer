# TX-Observer Project Guide

## 專案核心設定 (Identity)
- **語系規範**：強制使用 **繁體中文 (zh-TW)** 進行回覆。除非使用者特別要求，否則禁止使用簡體中文、英文、韓文或日文。
- **技術背景**：這是一個基於 Python 與 Shioaji API 的台股自動監控系統。
- **目標標的**：台指期近全 (TXFR1)、加權指數 (TSE/001)、櫃買指數 (OTC/101)。

## 關鍵技術規格 (Standard Operating Procedures)
- **時間對齊 (XQ 標準)**：
    - 5K 分組：`offset='45min'`, `closed='right'`, `label='right'`。
    - 60K 分組：日盤 (08:45-13:45) 為 45 分切割；夜盤 (15:00-05:00) 為整點切割。
- **繪圖規範 (renderer.py)**：
    - 單一 `matplotlib Figure` + `GridSpec`；mplfinance 外部 axes 模式，無需 Pillow 拼合。
    - 垂直子圖：5K (上, ~56%) + 共享 Legend 列 + 60K (下, ~44%)。
    - 中文字體：必須確保使用 `NotoSansTC-Regular.ttf` 以免出現豆腐塊。
    - 顏色：紅漲綠跌 (Up: Red, Down: Green)。

## 常用的執行指令 (CLI)
- **啟動程式**：`python main.py`
- **虛擬環境**：`source venv/bin/activate`
- **安裝套件**：`pip install -r requirements.txt`
- **Git 同步**：`git pull origin main`

## 程式碼開發守則
- **Shioaji 狀態**：API Login 必須是單例模式 (Singleton) 或在 `main.py` 啟動時執行一次。
- **例外處理**：抓取單一品種失敗時，不可中斷整體排程，需紀錄 Log 並繼續執行下一個品種。
- **路徑處理**：使用 `pathlib` 確保 Linux (Ubuntu) 伺服器路徑相容性。