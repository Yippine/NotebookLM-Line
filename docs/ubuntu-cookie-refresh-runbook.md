# NotebookLM Cookie 自動刷新——Ubuntu 部署 Runbook

給之後把 [`scripts/nlm_cookie_refresh.py`](../scripts/nlm_cookie_refresh.py) 從目前的 Windows 主機搬到團隊共用的 Ubuntu 主機時照做的步驟。背景與設計取捨見該腳本本身的 docstring；本文件只列「動手要做什麼」。

## 為什麼需要這些步驟（先讀這段，別跳過）

這台 Ubuntu 是**團隊共用**機器，多人會 SSH 進來做各自的事。核心風險不是技術問題，是人的問題：這支腳本依賴一個「保持登入、不能被登出或清 cookie」的 Firefox session，如果它跟任何一個人的日常帳號混在一起，遲早會被不知情的人手滑弄壞。以下步驟的重點就是把這個 Firefox session 結構性地隔離開來，不要只靠團隊口頭約定「請不要碰」。

## 步驟

### 1. 建立專用系統帳號

```bash
sudo useradd -m -s /bin/bash nlm-bot
sudo chmod 700 /home/nlm-bot
```

所有跟這個用途相關的東西（Firefox profile、`~/.notebooklm/`、cron job）都活在這個帳號底下，不掛在任何團隊成員自己的帳號上。

### 2. 鎖權限

在 `/etc/sudoers.d/` 底下限制只有負責處理登入失效的 1-2 人能夠切換到這個帳號（`sudo -u nlm-bot -i`），其他團隊成員不需要、也不應該有這個權限。

### 3. 裝虛擬顯示器 + 遠端桌面（給人手動登入用）

```bash
sudo apt install -y xvfb x11vnc firefox
```

用 `x11vnc`（或 `xrdp`）讓負責的人能偶爾遠端連進來，手動用 Firefox 登入一次共用帳號的 Google 帳號。之後不需要一直連著看畫面，但 Firefox 行程本身要持續在背景跑（見下一步），Google 才會持續幫這個 session 保鮮。

### 4. 用 systemd service 讓 Firefox 常駐，不依賴任何人的 SSH session

```ini
# /etc/systemd/system/nlm-firefox.service
[Unit]
Description=NotebookLM cookie source Firefox (headless via Xvfb)

[Service]
User=nlm-bot
ExecStart=/bin/bash -c 'Xvfb :99 -screen 0 1280x1024x24 & sleep 2 && DISPLAY=:99 firefox'
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nlm-firefox
```

這樣即使負責登入的人 SSH 斷線、登出，Firefox 也不會跟著死掉。

### 5. 裝 Python 依賴

```bash
sudo -u nlm-bot -i
python3 -m pip install --user "notebooklm-py[cookies]>=0.8.0"
```

Linux 上 `rookiepy` 通常有現成的 wheel，理論上不會像 Windows 那次一樣需要額外裝 Rust 工具鏈——但實際裝的時候還是要看一次輸出確認沒有 fallback 去源碼編譯。

### 6. 手動測試一次

確認第 3 步已經用 Firefox 登入過共用帳號之後：

```bash
sudo -u nlm-bot -i
cd /path/to/repo
python3 scripts/nlm_cookie_refresh.py
```

看到每個 channel 印出 `rebind ok` 才算過（這次手動測試會真的跑完整套流程，因為此時 DB 裡通常還沒有任何 channel 被標成 `expired`——如果閘門擋下來什麼都沒發生，先手動把某個 channel 的 `nlm_health_status` 改成 `expired` 再重跑一次）。如果印出「找不到 storage_state.json」，先確認 Firefox 那個 profile 底下真的有登入過。

### 7. 排 cron

```bash
sudo -u nlm-bot crontab -e
```

加入：

```
* * * * * cd /path/to/repo && python3 scripts/nlm_cookie_refresh.py >> /var/log/nlm_cookie_refresh.log 2>&1
```

每分鐘跑一次，不是每半小時——這支腳本現在是「偵測到後端把某個 channel 標成 `expired` 才真的刷新」（見腳本 docstring 裡的 `any_channel_expired()`），沒有 channel 掛掉的那幾輪只是讀一次本機 SQLite 就結束，成本低到可以忽略。排密一點的意義純粹是縮短「後端偵測到掛了」到「這支腳本真的去刷新」之間的延遲，不會因此變得比較吵。

### 8. 確認告警有接上

`nlm_cookie_refresh.py` 失敗時會讀 `backend/.env` 裡的 `ADMIN_LINE_USER_ID`/`ADMIN_ALERT_ACCESS_TOKEN` 發 LINE 通知——確認這台 Ubuntu 上的 `backend/.env` 也填了這兩個值（跟 Windows 主機上的一樣）。這是最後一道防線：就算前面的隔離措施還是被誰不小心弄壞，下一次真的偵測到失效時你會馬上收到通知，而不是等學員反映才發現。正常運作時完全靜默；同一個尚未解決的失敗原因也有 30 分鐘告警冷卻（見腳本 `_ALERT_COOLDOWN_SECONDS`），不會因為排程改密就被同一個問題洗版。

## 之後如果要更進一步隔離

如果這台 Ubuntu 主機本身業務很雜、變動頻繁，帳號層級的隔離可能不夠穩妥，可以考慮把整個 `nlm-bot` 的工作（Firefox + cron）搬進一個獨立的 LXC container 或小型 VM，讓「不要碰」直接變成「你沒有權限碰」，不再依賴任何人的自律。
