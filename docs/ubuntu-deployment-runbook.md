# 部署到 Ubuntu 主機——Runbook（透過 RustDesk 遠端桌面操作）

給要把這一套從目前的 Windows 主機搬到 Ubuntu 主機時照做的步驟。假設你是透過 RustDesk 連進一台**已經有桌面環境**的 Ubuntu（RustDesk 本身就需要一個正在跑的顯示器，所以不像純 SSH headless 主機那樣需要另外搭 Xvfb/VNC）。以下指令請在 RustDesk 連進去之後，開一個終端機執行。

## 總覽

跟 Windows 上這一套的差異只有三處：
1. cloudflared 的安裝方式不同（Windows 是裝好的執行檔，Ubuntu 要另外裝）
2. `notebooklm login` 綁定時要注意「後端跑在 Docker 容器裡看不到主機檔案系統」這個坑（我們在 Windows 上也踩過一次）
3. `tunnel_watchdog.py` 跟 `nlm_cookie_refresh.py` 建議用 systemd 常駐，而不是像 Windows 那樣開一個終端機視窗手動跑著——RustDesk 斷線或視窗被關掉，systemd 管理的行程不會跟著死掉

## 1. 安裝基礎環境

```bash
# Docker + Docker Compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker   # 讓當前 shell 立即套用 docker 群組，不用重新登入

# cloudflared
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
cloudflared --version   # 確認安裝成功

# Python（給 tunnel_watchdog.py / nlm_cookie_refresh.py 用，不需要進容器）
sudo apt install -y python3 python3-pip python3-venv firefox
```

## 2. 抓專案 + 設定 .env

```bash
git clone https://github.com/Yippine/NotebookLM-Line.git
cd NotebookLM-Line
cp backend/.env.example backend/.env
```

編輯 `backend/.env`，至少要填：

```env
ENCRYPTION_KEY=<python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 產生>
WEBHOOK_BASE_URL=https://placeholder.trycloudflare.com   # 之後會被 tunnel_watchdog.py 自動改寫，先隨便填一個佔位值
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

## 4. 啟動 Cloudflare Tunnel（建議用 systemd 常駐）

先確認 `docker compose ps` 顯示服務正常之後：

```bash
sudo tee /etc/systemd/system/nlm-tunnel-watchdog.service > /dev/null <<'EOF'
[Unit]
Description=NotebookLM-Line cloudflared tunnel watchdog
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/path/to/NotebookLM-Line
ExecStart=/usr/bin/python3 scripts/tunnel_watchdog.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 把 /path/to/NotebookLM-Line 換成實際路徑（例如 /home/<user>/NotebookLM-Line）
sudo systemctl daemon-reload
sudo systemctl enable --now nlm-tunnel-watchdog

journalctl -u nlm-tunnel-watchdog -f   # 看即時 log，確認拿到 tunnel URL、.env 被更新、LINE webhook 同步成功
```

這樣即使 RustDesk 斷線，tunnel 跟自動修復都會持續運作，不需要一直開著一個終端機視窗（這是跟 Windows 上目前做法最大的差異）。

> ⚠️ 如果短時間內重複手動測試 cloudflared 導致 429 rate limit（我們在 Windows 上就踩過這個），systemd 的 `Restart=on-failure` + `RestartSec=10` 加上腳本自己的 30 秒退避，可能會讓封鎖持續更久。第一次啟動時建議先用 `python3 scripts/tunnel_watchdog.py` 手動跑過一次確認能拿到 tunnel，再交給 systemd 接手常駐。

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
# 加入：
*/30 * * * * cd /path/to/NotebookLM-Line && python3 scripts/nlm_cookie_refresh.py >> /var/log/nlm_cookie_refresh.log 2>&1
```

**前提**：這台機器要有一個持續存在的桌面 session（Firefox 才讀得到已登入的 cookie）。如果 Ubuntu 設定成沒人用 RustDesk 連著就自動登出桌面，這支腳本會找不到已登入的 Firefox session——確認顯示管理器（GDM/LightDM）有設定自動登入，或桌面 session 不會因為斷開遠端連線就跟著結束。

## 7. LINE Webhook

`tunnel_watchdog.py` 拿到新的 tunnel URL 後，會自動透過 Messaging API 把 webhook endpoint 推給每個已綁定的 channel，不需要手動貼到 LINE Developers Console。第一次設定時到 `http://<主機IP或localhost>:8083/setup` 走一次管理介面流程即可。

## 檢查清單

- [ ] `docker compose ps` 兩個容器都 Up
- [ ] `nlm-tunnel-watchdog.service` 是 `active (running)`，log 裡看得到 `tunnel up`
- [ ] NotebookLM 已綁定，`/setup` 頁面能看到筆記本清單
- [ ] `nlm_cookie_refresh.py` 手動跑過一次成功，cron 已排好
- [ ] `backend/.env` 的 `ADMIN_LINE_USER_ID` / `ADMIN_ALERT_ACCESS_TOKEN` 已填，且你本人已加該 LINE 官方帳號好友（收得到告警 push）
