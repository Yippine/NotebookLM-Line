# 部署到 Ubuntu 主機——Runbook（透過 RustDesk 遠端桌面操作）

給要把這一套從目前的 Windows 主機搬到 Ubuntu 主機時照做的步驟。假設你是透過 RustDesk 連進一台**已經有桌面環境**的 Ubuntu（RustDesk 本身就需要一個正在跑的顯示器，所以不像純 SSH headless 主機那樣需要另外搭 Xvfb/VNC）。以下指令請在 RustDesk 連進去之後，開一個終端機執行。

> 這份文件已經改成用 **Tailscale Funnel** 對外曝露服務，不再用 cloudflared 的匿名 quick tunnel——後者在 Windows 上實際使用時，多次發生「候選 tunnel 被 Cloudflare 限流、整段時間連不上」的狀況，而且每次重啟網址都會換，需要一整套 watchdog（`tunnel_watchdog.py`）去偵測變化、改寫 `.env`、重新同步 LINE webhook。Tailscale Funnel 分配到的網址是固定不變的（跟你的 Tailscale 帳號、這台主機的機器名稱綁定），不會重啟就換，也不受那種匿名申請限流影響，watchdog 那一整套邏輯直接不需要了。

## 總覽

跟 Windows 上這一套的差異：
1. Tailscale 的安裝方式不同（Windows 是裝 Windows 版用戶端，Ubuntu 用官方腳本安裝，兩邊都要各自 `tailscale up` 登入、各自的機器會有各自的網址）
2. `notebooklm login` 綁定時要注意「後端跑在 Docker 容器裡看不到主機檔案系統」這個坑（我們在 Windows 上也踩過一次）
3. `nlm_cookie_refresh.py` 跟新的 `service_health_check.py` 建議用 cron 常駐，而不是像 Windows 那樣開一個終端機視窗手動跑著——RustDesk 斷線或視窗被關掉，cron 排的工作不會跟著死掉
4. 對外曝露服務改用 Tailscale Funnel，網址固定不變，**不需要**額外的 watchdog 行程去監看/自動修復

## 1. 安裝基礎環境

```bash
# Docker + Docker Compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker   # 讓當前 shell 立即套用 docker 群組，不用重新登入

# Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up   # 會印出一個 https://login.tailscale.com/a/... 網址，用瀏覽器打開登入你的 Tailscale 帳號
sudo tailscale status   # 確認顯示已連上，狀態不是 Stopped

# Python（給 nlm_cookie_refresh.py / service_health_check.py 用，不需要進容器）
sudo apt install -y python3 python3-pip python3-venv firefox
```

安裝腳本會自動把 `tailscaled` 註冊成 systemd 服務、開機自動啟動，不需要另外設定常駐。

## 2. 抓專案 + 設定 .env

```bash
git clone https://github.com/Yippine/NotebookLM-Line.git
cd NotebookLM-Line
cp backend/.env.example backend/.env
```

編輯 `backend/.env`，至少要填：

```env
ENCRYPTION_KEY=<python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 產生>
WEBHOOK_BASE_URL=https://placeholder.ts.net   # 第 4 步拿到 Funnel 的固定網址後，回來改成實際值
ADMIN_PASSWORD=<管理介面密碼>
ADMIN_LINE_USER_ID=<要收告警的人的 LINE userId，可逗號分隔多人>
ADMIN_ALERT_ACCESS_TOKEN=<任一已綁定頻道的 channel access token>
```

## 3. 啟動 Docker 服務

```bash
docker compose up -d --build
docker compose ps   # 確認 nlm-line-backend / nlm-line-frontend 都是 Up
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8083/   # 應該回 200
```

`8083` 這個 port 是 `docker-compose.yml` 裡 `APP_PORT` 的預設值，可在 `.env` 加一行 `APP_PORT=xxxx` 覆蓋。

## 4. 啟用 Tailscale Funnel（一次性設定，之後網址不會再變）

Funnel 這個功能預設沒開，第一次要先在網頁上啟用一次（每個 Tailscale 帳號只需要做一次，不是每台機器都要重新做——但如果啟用連結沒有自動偵測到已經開過，照著連結走一次也無妨）：

```bash
sudo tailscale funnel --bg 8083
```

如果印出「Funnel is not enabled on your tailnet」，會附上一個 `https://login.tailscale.com/f/funnel?node=...` 連結，用瀏覽器打開、確認帳號後即可，看到「Tailscale Funnel is ready to use」代表完成，回頭再執行一次上面那行指令。

成功會印出類似：

```
Available on the internet:
https://<這台主機的機器名稱>.<你的 tailnet 名稱>.ts.net/
|-- proxy http://127.0.0.1:8083
```

這個網址就是固定不變的公開網址（跟這台主機的機器名稱綁定，只要不改機器名稱、不換 Tailscale 帳號就不會變）。`--bg` 讓這個設定由 `tailscaled` 自己持久記住並在背景執行，不需要另外包一層 systemd unit，重開機也會自動恢復。

驗證：

```bash
tailscale funnel status   # 確認顯示 "Funnel on"
curl -sS -o /dev/null -w "%{http_code}\n" https://<機器名稱>.<tailnet 名稱>.ts.net/   # 應該回 200
```

拿到這個網址後，回去把 `backend/.env` 的 `WEBHOOK_BASE_URL` 改成實際值，然後：

```bash
docker compose up -d backend   # 重啟後端套用新的 WEBHOOK_BASE_URL
```

## 5. NotebookLM 登入 + 綁定

```bash
pip install --user "notebooklm-py[cookies]>=0.8.0"
python3 -m notebooklm login
```

會跳出瀏覽器視窗（透過 RustDesk 看得到）——登入課程共用帳號的 Google。登入完成後會存到 `~/.notebooklm/profiles/default/storage_state.json`。

**注意**：後端是跑在 Docker 容器裡，`docker-compose.yml` 沒有把主機家目錄掛進容器，容器看不到這個檔案，`nlm-bind-local` 端點在 Ubuntu 上一樣會找不到檔案。用跟 Windows 上一樣的方式，直接在主機端讀檔案內容、POST 給後端：

```bash
python3 - <<'PYEOF'
import json, urllib.request

CHANNEL_ID = "your-channel-id"   # 換成實際 channel_id
path = "/home/YOUR_USER/.notebooklm/profiles/default/storage_state.json"

with open(path, "r", encoding="utf-8") as f:
    storage_state = json.load(f)

body = json.dumps({"storage_state_json": storage_state}).encode("utf-8")
req = urllib.request.Request(
    f"http://localhost:8083/api/channels/{CHANNEL_ID}/nlm-login",
    data=body, method="POST", headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    print(json.loads(resp.read().decode("utf-8")))
PYEOF
```

綁定成功後，記得比照 Windows 上的做法，把本機那份含 cookie 的 `storage_state.json` 覆寫後刪除（用完即丟，不留在磁碟上）。

## 6. Cookie 自動續期（讓 Google 登入不用常常手動重來）

完整步驟見 [`ubuntu-cookie-refresh-runbook.md`](./ubuntu-cookie-refresh-runbook.md)，但既然你是透過 RustDesk 用真正的桌面環境（不是共用團隊機器），可以簡化：

- **可以跳過**該文件第 1-4 步的「建立專用系統帳號 + Xvfb + x11vnc」——這些是為了團隊共用機器，多人 SSH 情境下才需要的隔離措施。你這台如果是專用機器、只有你自己用 RustDesk 連，直接讓 Firefox 常駐在目前這個桌面 session 就好，登入一次、不要登出。
- 該文件第 5 步開始（裝 Python 依賴、手動測試、排 cron）都適用，照做即可：

```bash
python3 -m pip install --user "notebooklm-py[cookies]>=0.8.0"
python3 scripts/nlm_cookie_refresh.py   # 手動測試一次，確認印出 rebind ok

crontab -e
# 加入（每分鐘檢查一次；沒有 channel 被標成失效的那幾輪只讀一次本機
# SQLite 就結束，成本可以忽略，排密一點只是讓真正失效時反應更快）：
* * * * * cd /path/to/NotebookLM-Line && python3 scripts/nlm_cookie_refresh.py >> /var/log/nlm_cookie_refresh.log 2>&1
```

**前提**：這台機器要有一個持續存在的桌面 session（Firefox 才讀得到已登入的 cookie）。如果 Ubuntu 設定成沒人用 RustDesk 連著就自動登出桌面，這支腳本會找不到已登入的 Firefox session——確認顯示管理器（GDM/LightDM）有設定自動登入，或桌面 session 不會因為斷開遠端連線就跟著結束。

## 7. LINE Webhook（一次性設定，網址固定後不用再改）

因為 Funnel 網址不會再變，這裡只需要設定**一次**，不像 cloudflared 時代需要 watchdog 持續自動同步：

```bash
python3 - <<'PYEOF'
import sqlite3, json, urllib.request

CHANNEL_ID = "your-channel-id"      # 換成實際 channel_id
WEBHOOK_BASE_URL = "https://<機器名稱>.<tailnet 名稱>.ts.net"   # 換成第 4 步拿到的網址

conn = sqlite3.connect("data/data.db")
token = conn.execute(
    "SELECT channel_access_token FROM channels WHERE channel_id=?", (CHANNEL_ID,)
).fetchone()[0]

body = json.dumps({"endpoint": f"{WEBHOOK_BASE_URL}/webhook/{CHANNEL_ID}"}).encode("utf-8")
req = urllib.request.Request(
    "https://api.line.me/v2/bot/channel/webhook/endpoint",
    data=body, method="PUT",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=10) as resp:
    print(resp.status, resp.read())
PYEOF
```

## 8. 服務健康檢查（取代舊的 watchdog，只做通知、不做自動修復）

用 [`scripts/service_health_check.py`](../scripts/service_health_check.py) 定期檢查 Docker 服務跟 Funnel 是否還連得上，異常才會發 LINE 告警給管理員（狀態沒變化不會重複發送，避免灌爆每月推播額度）：

```bash
python3 scripts/service_health_check.py   # 手動測試一次

crontab -e
# 加入（例如每 10 分鐘一次）：
*/10 * * * * cd /path/to/NotebookLM-Line && python3 scripts/service_health_check.py >> /var/log/nlm_health_check.log 2>&1
```

這支腳本檢查到異常時只會通知，實際修復（Docker Desktop/daemon 掛掉、Tailscale 服務掛掉）還是要人到主機前處理。

## 檢查清單

- [ ] `docker compose ps` 兩個容器都 Up
- [ ] `tailscale funnel status` 顯示 "Funnel on"，`curl` 該網址回 200
- [ ] `backend/.env` 的 `WEBHOOK_BASE_URL` 已經是實際的 Funnel 網址（不是佔位值），且後端已用新值重啟過
- [ ] NotebookLM 已綁定，`/setup` 頁面能看到筆記本清單
- [ ] LINE 後台（`GET /v2/bot/channel/webhook/endpoint`）確認 endpoint 是新網址、`active: true`
- [ ] `nlm_cookie_refresh.py` 手動跑過一次成功，cron 已排好
- [ ] `service_health_check.py` 手動跑過一次成功，cron 已排好
- [ ] `backend/.env` 的 `ADMIN_LINE_USER_ID` / `ADMIN_ALERT_ACCESS_TOKEN` 已填，且你本人已加該 LINE 官方帳號好友（收得到告警 push）
