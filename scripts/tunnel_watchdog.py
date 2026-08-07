"""讓 cloudflared 的 quick tunnel 保持存活並能自我修復。

免費的 quick tunnel（``cloudflared tunnel --url ...``）沒有任何
上線時間保證：Cloudflare 可能會在本機行程仍在正常執行、
沒有任何錯誤的情況下，悄悄地把 DNS 註冊給撤掉，或者 tunnel
本身也可能直接結束。這個看門狗程式會：

  1. 啟動（或重啟）一個指向 localhost:8083 的 quick tunnel。
  2. 定期對被分配到的主機名稱做健康檢查。
  3. 每當主機名稱發生變化時（第一次啟動，或是在一次無聲死亡
     後重啟），自動執行以下動作：
       a. 更新 backend/.env 中的 WEBHOOK_BASE_URL
       b. 重新建立後端容器（`docker compose up -d`），讓它套用
          新的設定值
       c. 透過 Messaging API（PUT /v2/bot/channel/webhook/endpoint）
          將新的 webhook URL 推送給每一個已綁定的 channel——
          不需要手動貼到 LINE Developers Console。

在終端機中執行一次，讓它持續在背景執行即可：
    python scripts/tunnel_watchdog.py
"""

import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / "backend" / ".env"
DB_PATH = ROOT / "data" / "data.db"
LOCAL_PORT = 8083
CHECK_INTERVAL_SECONDS = 60
HOSTNAME_TIMEOUT_SECONDS = 20
LINE_ENDPOINT_API = "https://api.line.me/v2/bot/channel/webhook/endpoint"

# 真正的 quick-tunnel 主機名稱永遠是好幾個以連字號分隔的隨機單字
# （例如 "reward-clearance-answer-authorities.trycloudflare.com"）。
# cloudflared 在非 TTY（被 pipe）的輸出中，在真正的主機名稱被
# 分配之前，也可能先印出一個像 "api.trycloudflare.com" 這樣的
# 陽春佔位主機名稱——要求至少 3 個區段，讓這個誘餌永遠不會比對成功。
_HOSTNAME_RE = re.compile(r"https://([a-z0-9]+(?:-[a-z0-9]+){2,}\.trycloudflare\.com)")


def log(msg: str) -> None:
    print(f"[watchdog] {msg}", flush=True)


def _drain(stream) -> None:
    """持續讀取子行程的 stdout，避免它的 pipe 緩衝區被填滿。"""
    for _ in stream:
        pass


def start_tunnel() -> tuple[subprocess.Popen, str]:
    """啟動 cloudflared，並阻塞直到它回報被分配到的主機名稱為止。"""
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{LOCAL_PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    hostname = None
    deadline = time.time() + HOSTNAME_TIMEOUT_SECONDS
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            continue
        match = _HOSTNAME_RE.search(line)
        if match:
            hostname = match.group(1)
            break

    if not hostname:
        proc.kill()
        raise RuntimeError("cloudflared did not report a hostname in time")

    threading.Thread(target=_drain, args=(proc.stdout,), daemon=True).start()
    return proc, hostname


# 這些是 Cloudflare 邊緣節點的錯誤碼，代表「有成功連上
# Cloudflare，但它連不到本機的 tunnel/origin」——這是 tunnel
# 死掉了，不是應用程式本身壞掉。
# https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/
_TUNNEL_DEAD_HTTP_CODES = {502, 521, 522, 523, 524, 530}


def is_dns_alive(hostname: str) -> bool:
    """只有在 tunnel 確實有將流量轉發到本機應用程式時才回傳 True。

    來自*我們自己應用程式*的一般 HTTP 錯誤（例如簽章錯誤導致的
    403）代表 tunnel 本身是正常的。而 Cloudflare 邊緣錯誤
    （例如 530）代表 tunnel 的註冊確實存在，但實際上沒有連到
    任何東西——這是一個披著存活主機名稱外皮的死掉 tunnel。
    """
    try:
        urllib.request.urlopen(f"https://{hostname}/", timeout=10)
        return True
    except urllib.error.HTTPError as e:
        if e.code in _TUNNEL_DEAD_HTTP_CODES:
            log(f"{hostname} responded but tunnel is unreachable (HTTP {e.code})")
            return False
        return True
    except Exception as e:
        log(f"health check failed for {hostname}: {e}")
        return False


def update_env_webhook_url(new_url: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8")
    new_text = re.sub(r"(?m)^WEBHOOK_BASE_URL=.*$", f"WEBHOOK_BASE_URL={new_url}", text)
    ENV_PATH.write_text(new_text, encoding="utf-8")


def _read_env_var(name: str) -> str:
    """最陽春的 .env 行讀取器——這個腳本只在乎一兩個值，
    不需要為此引入 python-dotenv 依賴。"""
    if not ENV_PATH.exists():
        return ""
    match = re.search(rf"(?m)^{name}=(.*)$", ENV_PATH.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else ""


def send_admin_alert(message: str) -> None:
    """盡力而為地推送 LINE 訊息給管理員，讓即使沒有人正在盯著這個
    終端機，tunnel 死掉這件事也會被知道。若管理員告警未設定
    （ADMIN_LINE_USER_ID / ADMIN_ALERT_ACCESS_TOKEN 未設定），
    則不做任何事。

    這裡只涵蓋「tunnel 死掉、而看門狗程式本身還活著能發現它」
    的情況——如果看門狗行程本身被砍掉（例如機器進入睡眠，或
    執行它的終端機被關閉），這裡的機制完全不會被觸發。那種
    失效模式需要作業系統層級的行程監控（例如設定成失敗時自動
    重啟的 Windows 排程工作），而不是應用程式層級的告警。
    """
    admin_user_id = _read_env_var("ADMIN_LINE_USER_ID")
    access_token = _read_env_var("ADMIN_ALERT_ACCESS_TOKEN")
    if not admin_user_id or not access_token:
        return

    body = json.dumps(
        {"to": admin_user_id, "messages": [{"type": "text", "text": message}]}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"failed to send admin alert: {e}")


def restart_backend() -> None:
    subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT, check=True)


def sync_line_webhook_urls(new_base_url: str) -> None:
    if not DB_PATH.exists():
        log(f"no database at {DB_PATH} yet, skipping LINE sync")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT channel_id, channel_access_token FROM channels"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        endpoint = f"{new_base_url}/webhook/{row['channel_id']}"
        body = json.dumps({"endpoint": endpoint}).encode("utf-8")
        req = urllib.request.Request(
            LINE_ENDPOINT_API,
            data=body,
            method="PUT",
            headers={
                "Authorization": f"Bearer {row['channel_access_token']}",
                "Content-Type": "application/json",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            log(f"synced LINE webhook for {row['channel_id']} -> {endpoint}")
        except Exception as e:
            log(f"FAILED to sync LINE webhook for {row['channel_id']}: {e}")


def wait_until_reachable(hostname: str, tries: int = 8, delay: float = 4.0) -> bool:
    """Quick tunnel 建立後可能需要幾秒鐘才能連得上。在把一個
    剛啟動的 tunnel 判定為壞掉之前先重試幾次——絕不能只憑
    單一次失敗的檢查，就去動 .env/backend/LINE。"""
    for attempt in range(1, tries + 1):
        if is_dns_alive(hostname):
            return True
        log(f"{hostname} not reachable yet (attempt {attempt}/{tries})")
        time.sleep(delay)
    return False


def start_healthy_tunnel(max_attempts: int = 5) -> tuple[subprocess.Popen, str]:
    """啟動一個 tunnel，並在回傳它之前先確認它真的連得上。

    有些 quick-tunnel 註冊完全不會變得可連線（即使過了完整的
    寬限期，DNS 仍是「網域不存在」）——這是一個壞掉的 tunnel，
    而不是慢的 tunnel。應該直接丟棄它並要求一個全新的，而不是
    卡在等待一個永遠不會解析成功的註冊上。cloudflared 本身偶爾
    也會連主機名稱都來不及回報——這也算是一次失敗的嘗試，
    而不是一個致命錯誤。
    """
    for attempt in range(1, max_attempts + 1):
        try:
            proc, hostname = start_tunnel()
        except RuntimeError as e:
            log(f"tunnel candidate failed to start (attempt {attempt}/{max_attempts}): {e}")
            continue
        log(f"tunnel candidate: https://{hostname} (attempt {attempt}/{max_attempts})")
        if wait_until_reachable(hostname):
            return proc, hostname
        log(f"{hostname} never became reachable, discarding and requesting a fresh tunnel")
        proc.kill()
    raise RuntimeError(f"failed to get a reachable tunnel after {max_attempts} attempts")


def _retry_forever(description: str, fn, backoff_seconds: float = 30.0):
    """持續呼叫 ``fn`` 直到成功為止，失敗時記錄並延遲重試，
    而不是往外拋出例外。

    一個會被自己的暫時性失敗（cloudflared 啟動失敗、
    ``docker compose up`` 出點小狀況、LINE API 不穩定）搞死的
    看門狗程式，一旦死掉就等於什麼都沒在看守了——每一個復原
    步驟都必須能夠承受無限次重試，而不是讓行程崩潰、
    悄悄地讓 webhook 一直處於失效狀態，直到有人發現為止。
    """
    while True:
        try:
            return fn()
        except Exception as e:
            log(f"{description} failed ({e}); retrying in {backoff_seconds:.0f}s")
            time.sleep(backoff_seconds)


def apply_new_hostname(hostname: str) -> None:
    url = f"https://{hostname}"
    log(f"applying new tunnel URL: {url}")
    update_env_webhook_url(url)
    restart_backend()
    sync_line_webhook_urls(url)
    log("done — .env updated, backend restarted, LINE channels synced")


def main() -> None:
    proc, hostname = _retry_forever("establishing a healthy tunnel", start_healthy_tunnel)
    log(f"tunnel up: https://{hostname}")
    _retry_forever("applying new tunnel hostname", lambda: apply_new_hostname(hostname))

    try:
        while True:
            time.sleep(CHECK_INTERVAL_SECONDS)
            if not is_dns_alive(hostname):
                log("tunnel appears dead, restarting cloudflared...")
                proc.kill()
                proc, hostname = _retry_forever("establishing a healthy tunnel", start_healthy_tunnel)
                log(f"tunnel back up: https://{hostname}")
                _retry_forever("applying new tunnel hostname", lambda: apply_new_hostname(hostname))
                send_admin_alert(
                    f"[watchdog] tunnel 曾經掛掉，已自動恢復為新網址：https://{hostname}"
                )
    except KeyboardInterrupt:
        log("stopping, killing tunnel process")
        proc.kill()
        sys.exit(0)


if __name__ == "__main__":
    main()
