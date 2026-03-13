我已經為您建立了一個全新的專案資料夾 simple-ios-location，並在其中實作了最精簡的 iOS 位置模擬程式。

這個程式包含了兩個主要功能：

自動判斷 iOS 版本：
若是 iOS 17+：會自動建立安全通道 (Tunnel) 並連線到 RSD 服務 (這是新版 iOS 修改定位的必要步驟)。
若是舊版 iOS：使用標準的 Lockdown 服務。
自動掛載 Developer Image：这是修改定位的前提，程式會嘗試自動完成。
此專案位於： simple-ios-location

檔案結構
requirements.txt: 依賴套件列表 (主要是 pymobiledevice3)。
simple_location.py: 核心 Python 程式碼，僅約 100 行，去除了所有 GUI 和 Web Server 的複雜邏輯。
如何使用
進入資料夾：

安裝依賴 (建議使用虛擬環境)：

執行程式 (請確保 iPhone 已解鎖並信任電腦)：

MacOS (通常需要 sudo 權限來存取 USB)：
如果不加參數，預設會定位到台北 101。
停止模擬：
程式執行後會保持連線，按 Ctrl+C 即可停止模擬並自動恢復原本位置。

python3 -m venv venv

source venv/bin/activate

sudo python3 simple_location.py --lat 24.9766463 --lng 121.529733
