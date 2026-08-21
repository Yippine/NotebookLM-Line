# 部署到 Ubuntu 主機——Runbook（透過 RustDesk 遠端桌面操作）

給要把這一套從目前的 Windows 主機搬到 Ubuntu 主機時照做的步驟。假設你是透過 RustDesk 連進一台**已經有桌面環境**的 Ubuntu（RustDesk 本身就需要一個正在跑的顯示器，所以不像純 SSH headless 主機那樣需要另外搭 Xvfb/VNC）。以下指令請在 RustDesk 連進去之後，開一個終端機執行。

> 這份文件已經改成用**具名的 Cloudflare Tunnel**（綁定專案自己的網域）對外曝露服務，不再用 cloudflared 的匿名 quick tunnel，也不再用 Tailscale Funnel。最早用的是 cloudflared 匿名 quick tunnel——在 Windows 上實際使用時，多次發生「候選 tunnel 被 Cloudflare 限流、整段時間連不上」的狀況，而且每次重啟網址都會換，需要一整套 watchdog（`tunnel_watchdog.py`）去偵測變化、改寫 `.env`、重新同步 LINE webhook；後來一度改用 Tailscale Funnel 換取固定網址。現在專案已經有了自己具名的 Cloudflare 網域——具名 tunnel（跟匿名 quick tunnel 不同）的網址一樣是固定不變的（綁在你自己的網域、你自己的 Cloudflare 帳號），不受匿名申請限流影響，也不會重啟就換，所以改回用 Cloudflare、不再需要 Tailscale，watchdog 那一整套邏輯一樣不需要。

## 總覽

跟 Windows 上這一套的差異：
1. `notebooklm login` 綁定時要注意「後端跑在 Docker 容器裡看不到主機檔案系統」這個坑（我們在 Windows 上也踩過一次）
2. `nlm_cookie_refresh.py` 跟新的 `service_health_check.py` 建議用 cron 常駐，而不是像 Windows 那樣開一個終端機視窗手動跑著——RustDesk 斷線或視窗被關掉，cron 排的工作不會跟著死掉
3. 對外曝露服務用具名的 Cloudflare Tunnel，網址固定不變（綁在專案自己的網域上），**不需要**額外的 watchdog 行程去監看/自動修復

## 1. 安裝基礎環境

```bash
# Docker + Docker Compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker   # 讓當前 shell 立即套用 docker 群組，不用重新登入

# cloudflared（官方 apt repo，裝好之後才有 `cloudflared` 指令可用）
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install -y cloudflared
cloudflared --version   # 確認安裝成功

# Python（給 nlm_cookie_refresh.py / service_health_check.py 用，不需要進容器）
sudo apt install -y python3 python3-pip python3-venv firefox
```

完整的 Cloudflare Tunnel 登入、建立、常駐設定在第 4 步，這裡只先把 `cloudflared` 指令裝起來。

## 2. 抓專案 + 設定 .env

```bash
git clone https://github.com/Yippine/NotebookLM-Line.git
cd NotebookLM-Line
cp backend/.env.example backend/.env
```

編輯 `backend/.env`，至少要填：

```env
ENCRYPTION_KEY=<python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 產生>
WEBHOOK_BASE_URL=https://placeholder.example.com   # 第 4 步設定好 Cloudflare Tunnel 的固定網域後，回來改成實際值
ADMIN_PASSWORD=<管理介面密碼>
ADMIN_LINE_USER_ID=<要收告警的人的 LINE userId，可逗號分隔多人>
ADMIN_ALERT_ACCESS_TOKEN=<任一已綁定頻道的 channel access token>
```

管理員告警預設走 LINE push，額外還能加開 Google Chat、Telegram 兩條管道（三者互不影響、可以只開其中一種也可以三個都開）——設定方式見第 9 步，不是部署當下就一定要做，先留空即可。

## 3. 啟動 Docker 服務

```bash
docker compose up -d --build
docker compose ps   # 確認 nlm-line-backend / nlm-line-frontend 都是 Up
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8083/   # 應該回 200
```

`8083` 這個 port 是 `docker-compose.yml` 裡 `APP_PORT` 的預設值，可在 `.env` 加一行 `APP_PORT=xxxx` 覆蓋。

這是**第一次**啟動、容器還不存在的情況，直接用 `docker compose` 沒問題。**之後**只要是「程式碼有更新、backend 容器本來就已經在跑」的重新部署，一律改用第 10 步的 `scripts/deploy_backend.py`，不要再直接下 `docker compose build/up`——原因見第 10 步。

## 4. 設定具名 Cloudflare Tunnel（一次性設定，之後網址不會再變）

下面的 `<你的網域>` 換成專案實際的 Cloudflare 網域（例如 `line-bot.example.com`），`<tunnel名稱>` 自己取一個好辨識的名字即可（例如 `nlm-line`）。

### 4.1 登入 Cloudflare 帳號

```bash
cloudflared tunnel login
```

會印出一個瀏覽器登入連結（透過 RustDesk 看得到），登入後選擇這個網域所在的 Cloudflare 帳號、授權即可。成功後憑證會存在 `~/.cloudflared/cert.pem`。

### 4.2 建立具名 tunnel

```bash
cloudflared tunnel create <tunnel名稱>
```

會印出一組 tunnel ID，並在 `~/.cloudflared/<tunnel ID>.json` 產生這個 tunnel 專屬的憑證檔——這個 tunnel ID 跟你自己建立的這個 tunnel 綁定，不會因為重啟或換機器就變。

### 4.3 設定路由設定檔

建立 `~/.cloudflared/config.yml`：

```yaml
tunnel: <tunnel名稱或上一步印出的 tunnel ID>
credentials-file: /home/<你的使用者名稱>/.cloudflared/<tunnel ID>.json

ingress:
  - hostname: <你的網域>
    service: http://localhost:8083
  - service: http_status:404
```

`ingress` 最後一定要有一條沒有 `hostname` 的規則（`http_status:404`）當作預設值，這是 cloudflared 的硬性要求，沒有的話啟動會直接失敗。

### 4.4 把網域路由到這個 tunnel

```bash
cloudflared tunnel route dns <tunnel名稱> <你的網域>
```

這一步會在 Cloudflare 的 DNS 幫你自動加一筆 CNAME 紀錄，把 `<你的網域>` 指到這個 tunnel——只要不刪掉這筆紀錄、不刪掉這個 tunnel，網址就不會變。

### 4.5 裝成 systemd 服務，開機自動啟動、斷線不會跟著死掉

```bash
# 用 sudo 執行時，cloudflared 找的是 root 的家目錄（/root/.cloudflared/），
# 不是你剛剛設定檔案所在的家目錄，一定要用 --config 明確指到實際路徑，
# 不然裝起來的服務會讀不到 4.3 步設定的 config.yml。
sudo cloudflared --config /home/<你的使用者名稱>/.cloudflared/config.yml service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared   # 確認是 active (running)
```

`cloudflared service install` 會把設定複製一份到系統層級（`/etc/cloudflared/`），並註冊成 systemd 服務——之後不需要留一個終端機視窗手動跑著，RustDesk 斷線也不會跟著死掉。

驗證：

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://<你的網域>/   # 應該回 200
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

因為具名 tunnel 的網址不會再變，這裡只需要設定**一次**，不像匿名 quick tunnel 時代需要 watchdog 持續自動同步：

```bash
python3 - <<'PYEOF'
import sqlite3, json, urllib.request

CHANNEL_ID = "your-channel-id"      # 換成實際 channel_id
WEBHOOK_BASE_URL = "https://<你的網域>"   # 換成第 4 步設定好的網域

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

用 [`scripts/service_health_check.py`](../scripts/service_health_check.py) 定期檢查 Docker 服務跟 Cloudflare Tunnel 是否還連得上，異常才會發 LINE 告警給管理員（狀態沒變化不會重複發送，避免灌爆每月推播額度）：

```bash
python3 scripts/service_health_check.py   # 手動測試一次

crontab -e
# 加入（例如每 10 分鐘一次）：
*/10 * * * * cd /path/to/NotebookLM-Line && python3 scripts/service_health_check.py >> /var/log/nlm_health_check.log 2>&1
```

這支腳本檢查到異常時只會通知，實際修復（Docker Desktop/daemon 掛掉、cloudflared 服務掛掉）還是要人到主機前處理——後者可以先試 `sudo systemctl restart cloudflared`，還是不行才需要重新走一次第 4 步排查。

## 9. 管理員告警：加開 Google Chat / Telegram 管道（可選）

第 2 步的 `ADMIN_LINE_USER_ID` / `ADMIN_ALERT_ACCESS_TOKEN` 已經足以讓所有告警（NotebookLM 查詢失敗、第 8 步的健康檢查異常、cookie 續期失敗等——全部都是同一個 `alert_service.notify_admin` 出口）用 LINE push 送出。以下兩條是**額外**的管道，三者彼此獨立、任一個沒填就自動不啟用該管道，不影響其他管道：

- **不佔用 LINE 官方帳號的每月訊息額度**——告警量大時尤其有感。
- 收告警的人不需要是任何 LINE 官方帳號的好友，避免漏加好友導致收不到告警。

兩條都是選配，看情況挑一個或兩個都開即可。

### Google Chat（走 Incoming Webhook）

在要接收告警的 Google Chat 空間裡，點「應用程式和整合」→「新增 Webhook」，取得一組網址，填入：

```env
GOOGLE_CHAT_WEBHOOK_URL=https://chat.googleapis.com/v1/spaces/xxx/messages?key=xxx&token=xxx
```

**注意**：這個「新增 Webhook」功能目前只開放給 Google Workspace 帳號建立的空間。如果空間是用個人 Gmail 帳號建立的，通常會顯示「Webhook 管理功能受到限制」而拿不到網址——這種情況請改用下面的 Telegram。

驗證（直接對網址發一則測試訊息，不需要重啟服務）：

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"text": "測試訊息，收到請忽略"}' \
  "$GOOGLE_CHAT_WEBHOOK_URL"
```

### Telegram（走 Bot API）

個人帳號就能申請，不像 Google Chat 的 Webhook 管理只開放給 Workspace 帳號：

1. 在 Telegram 跟 [@BotFather](https://t.me/BotFather) 對話，用 `/newbot` 建立一個 bot，依指示取得一組 token（即 `TELEGRAM_BOT_TOKEN`）。
2. 跟這個新建立的 bot 隨便傳一句話（**這一步不能省**——bot 沒被主動私訊過，下一步的 API 拿不到任何資料）。
3. 瀏覽器打開 `https://api.telegram.org/bot<TOKEN>/getUpdates`（`<TOKEN>` 換成上一步拿到的），從回應的 JSON 裡找 `"chat":{"id": ...}`，這組數字就是 `TELEGRAM_CHAT_ID`。

```env
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789
```

驗證：

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
  -H "Content-Type: application/json" \
  -d "{\"chat_id\": \"$TELEGRAM_CHAT_ID\", \"text\": \"測試訊息，收到請忽略\"}"
```

### 套用設定

改完 `backend/.env` 後跟第 4 步改 `WEBHOOK_BASE_URL` 一樣，要重啟後端才會讀到新值：

```bash
docker compose up -d backend
```

這裡只是重讀 `.env`、沒有動到程式碼，用 `docker compose` 直接重啟沒問題；如果你同時也有 pull 新程式碼，改用下一步的部署腳本即可，它一樣會重啟後端套用新的 `.env`。

## 10. 日常部署：之後程式碼有更新時

前面 1-9 步都是**一次性設定**，只有第一次架站時要做。之後每次 `git pull` 到新程式碼要上線，固定用這支腳本，不要再手動下 `docker compose build/up`：

```bash
cd /path/to/NotebookLM-Line
git pull
python3 scripts/deploy_backend.py
```

它比直接下 `docker compose` 多做兩件事，兩件都是真實事故換來的教訓：

1. **部署前檢查有沒有問題正在處理中**：backend 是同步阻塞式處理每個問題，收到訊息會一路等到 NotebookLM 查完才回覆（實測過最久 5 分半）。這段期間如果容器被重啟，查詢會被無聲打斷，使用者什麼都收不到、也不會有任何告警——2026-08-11/12 已經真的發生過兩次。腳本會先看最近的 log 有沒有問題還沒跑完，有的話先等，不會直接動手重啟。
2. **部署後自動重推 NotebookLM 人設**：`services/nlm_service.py` 裡的 `RESTRICTED_TOPIC_CUSTOM_PROMPT`（拒答規則、排版規則等）只有在筆記本「首次綁定」或「切換筆記本」時才會真正推送到 NotebookLM 那邊生效——單純改這段文字、重新部署，並不會讓已經綁定的 channel 自動套用新版。如果用 `docker compose` 直接重啟，這段文字更新完全不會生效，除非有人事後想到要手動呼叫 `/api/channels/{channel_id}/refresh-guard`（這是真實發生過的落差：改完排版規則、部署了，但已綁定的 channel 一直吐舊格式，因為沒有人手動重推）。腳本會在重啟成功後，自動對資料庫裡每一個已綁定 channel 呼叫這支 API，不需要記得手動做。

如果只是改 `.env`（沒有動程式碼），直接 `docker compose up -d backend` 重啟即可，不需要跑這支腳本——它的兩個保護都是針對「程式碼／prompt 有變動」的情境。

## 檢查清單

- [ ] `docker compose ps` 兩個容器都 Up
- [ ] `sudo systemctl status cloudflared` 顯示 active (running)，`curl` 該網域回 200
- [ ] `backend/.env` 的 `WEBHOOK_BASE_URL` 已經是實際的 Cloudflare 網域（不是佔位值），且後端已用新值重啟過
- [ ] NotebookLM 已綁定，`/setup` 頁面能看到筆記本清單
- [ ] LINE 後台（`GET /v2/bot/channel/webhook/endpoint`）確認 endpoint 是新網址、`active: true`
- [ ] `nlm_cookie_refresh.py` 手動跑過一次成功，cron 已排好
- [ ] `service_health_check.py` 手動跑過一次成功，cron 已排好
- [ ] `backend/.env` 的 `ADMIN_LINE_USER_ID` / `ADMIN_ALERT_ACCESS_TOKEN` 已填，且你本人已加該 LINE 官方帳號好友（收得到告警 push）
- [ ] （可選）Google Chat / Telegram 告警管道已依第 9 步設定並測試收得到訊息
- [ ] 之後的程式碼部署一律用 `python3 scripts/deploy_backend.py`（見第 10 步），不要直接下 `docker compose build/up` 略過查詢等待與人設重推
