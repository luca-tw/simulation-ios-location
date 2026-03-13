# iOS 位置模擬器 (Web 介面版)

這是一個基於 Python 與 Flask 的 iOS 位置模擬工具，透過 `pymobiledevice3` 實現與 iOS 裝置的通訊。本專案提供了一個簡單的網頁介面，讓使用者可以輕鬆設定或清除 iOS 裝置的 GPS 位置。

## 主要功能

*   **Web 操作介面**：透過瀏覽器即可輸入經緯度並修改裝置位置。
*   **支援新版 iOS**：自動偵測 iOS 版本。
    *   **iOS 17+**：支援建立 CoreDevice Tunnel 安全通道進行連線。
    *   **iOS < 17**：使用標準 Lockdown 服務。
*   **自動掛載映像檔**：自動檢查並掛載必要的 Developer Disk Image。
*   **位置恢復**：程式結束或手動清除後，自動恢復裝置至真實位置。
*   **非同步處理**：後端採用 Asyncio 確保連線穩定，避免介面卡頓。

## 系統需求

*   **作業系統**：建議使用 **macOS** (因 iOS 17+ 的 Tunnel 功能在非 macOS 環境可能受限，且通常需要 `sudo` 權限)。
*   **Python**：Python 3.10 或以上版本。
*   **iOS 裝置**：
    *   需開啟 **開發者模式 (Developer Mode)** (設定 -> 隱私權與安全性 -> 開發者模式)。
    *   需透過 USB 連接電腦，並點選「信任這部電腦」。

## 安裝步驟

1.  **複製專案**
    ```bash
    git clone <repository_url>
    cd simulation-ios-location
    ```

2.  **建立虛擬環境 (建議)**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  **安裝依賴套件**
    ```bash
    pip install -r requirements.txt
    ```

## 使用方法

1.  **連接裝置**
    將 iOS 裝置透過 USB 線連接至電腦，解鎖螢幕並確保已信任電腦。

2.  **執行程式**
    由於 iOS 17+ 的 Tunnel 建立通常需要較高權限來存取網路介面，建議使用 `sudo` 執行：

    ```bash
    # 請確保使用的是虛擬環境中的 python
    sudo ./venv/bin/python main.py
    
    # 或若未用虛擬環境
    sudo python3 main.py
    ```

3.  **開啟網頁介面**
    程式啟動後，打開瀏覽器存取：
    [http://127.0.0.1:8000](http://127.0.0.1:8000)

4.  **操作說明**
    *   輸入 **緯度 (Latitude)** 與 **經度 (Longitude)**，點擊「設定位置」即可模擬。
    *   點擊「清除位置」或關閉程式 (Ctrl+C)，裝置將恢復真實 GPS 定位。

## 常見問題與排除

*   **Permission denied / Operation not permitted**
    *   請確認是否使用了 `sudo` 執行程式。MacOS 對於 USB 裝置與虛擬網路介面的操作有嚴格限制。

*   **No module named 'pymobiledevice3'**
    *   請確認是否已進入虛擬環境 (`source venv/bin/activate`) 或套件是否安裝在全域環境中。若使用 `sudo`，環境變數可能會改變，建議使用完整路徑執行 Python (如 `sudo ./venv/bin/python main.py`)。

*   **DeviceLocked / InvalidHostID**
    *   請檢查裝置螢幕是否已解鎖。
    *   請確認是否已點擊「信任這部電腦」。
    *   若問題持續，請嘗試拔除 USB 線重新連接。

*   **iOS 17+ 連線逾時**
    *   建立 Tunnel 可能需要幾秒鐘的時間，若失敗請重試執行程式。

## 專案結構

*   `main.py`: 程式進入點。
*   `app/`
    *   `web/`: Flask 網頁介面路由。
    *   `api/`: RESTful API 路由。
    *   `services/location.py`: 核心邏輯，包含與 `pymobiledevice3` 的整合與 Asyncio 任務管理。
*   `templates/`: 網頁 HTML 模板。
