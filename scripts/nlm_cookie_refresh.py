"""定期從本機瀏覽器讀取已登入的 NotebookLM cookie，回寫給所有已綁定
的 channel，藉此降低「TrueExpiry」（整個 session 死掉）時需要人工
重新登入的頻率。

背景：NotebookLM 沒有官方 API，只能靠瀏覽器 session cookie
（storage_state.json）存取。這組 cookie 有兩種失效方式：

  - Rotation（正常換發）：已經全自動，每次開 client 套件都會自動
    跟 Google 換新值，不需要這支腳本處理。
  - TrueExpiry（整個 session 死掉）：目前唯一解法是人工重新登入。

這支腳本不能消除 TrueExpiry——桌面瀏覽器的登入本身還是會被
Google 風控清掉，只是比伺服器端純 cookie 的存活期長很多（經驗上
是天到週的量級）。它做的事情，是把「多久要人工重登一次」的頻率
降低，而不是把人工完全消除：只要這台機器上的瀏覽器本身還維持
登入，這支腳本就能定期把新鮮的 cookie 續回資料庫。

前提條件：
  - 這支腳本要在跟後端同一台機器上執行（本機讀瀏覽器 cookie，
    再打 localhost 的 admin API）
  - 該機器上的 Firefox 必須已經用共用帳號登入過 NotebookLM，並且
    保持登入——不要登出、不要清 cookie、不要換帳號登入。這件事
    沒有技術手段能強制保障，純粹要靠自己維持這個習慣
  - 需要額外安裝：pip install "notebooklm-py[cookies]"

用法（手動測試一次）：
    python scripts/nlm_cookie_refresh.py

排定成 Windows 排程工作（例如每 12 小時跑一次）：
    schtasks /create /tn "NLM Cookie Refresh" /sc hourly /mo 12 ^
        /tr "python C:\\path\\to\\scripts\\nlm_cookie_refresh.py" /rl highest
"""

import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / "backend" / ".env"
DB_PATH = ROOT / "data" / "data.db"
LOCAL_PORT = 8083
BROWSER = "firefox"
SUBPROCESS_TIMEOUT_SECONDS = 60


def log(msg: str) -> None:
    print(f"[nlm-cookie-refresh] {msg}", flush=True)


def _read_env_var(name: str) -> str:
    """最陽春的 .env 行讀取器，跟 tunnel_watchdog.py 用同一招——
    這個腳本只在乎一兩個值，不需要為此引入 python-dotenv 依賴。"""
    if not ENV_PATH.exists():
        return ""
    match = re.search(rf"(?m)^{name}=(.*)$", ENV_PATH.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else ""


def send_admin_alert(message: str) -> None:
    """盡力而為地推送 LINE 訊息給管理員。若管理員告警未設定
    （ADMIN_LINE_USER_ID / ADMIN_ALERT_ACCESS_TOKEN 未設定），
    則不做任何事。"""
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


def refresh_cookies() -> bool:
    """從本機已登入的瀏覽器讀取 cookie，寫成新的 storage_state.json。

    只記錄成功/失敗與簡短錯誤訊息，不記錄指令輸出的完整內容——
    這個指令本身不會印出 cookie 值，但避免日後有人在這裡加東西時
    不小心把敏感內容一起印進 log。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "notebooklm", "login", "--browser-cookies", BROWSER],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log(f"reading {BROWSER} cookies timed out after {SUBPROCESS_TIMEOUT_SECONDS}s")
        return False
    except Exception as e:
        log(f"failed to run notebooklm CLI: {e}")
        return False

    if result.returncode != 0:
        last_line = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
        log(f"notebooklm login --browser-cookies {BROWSER} failed: {last_line[0]}")
        return False

    log(f"refreshed cookies from local {BROWSER} session")
    return True


def _find_local_storage_state() -> str | None:
    """尋找這台主機上最近建立的 storage_state.json。

    跟 auth.py 的 _find_storage_state() 找的是同一種檔案，但那個
    函式是給「後端容器自己」讀它自己容器內檔案系統用的——這支
    腳本是在主機（不是容器）上執行 --browser-cookies，寫出來的
    檔案在主機的家目錄下，容器完全看不到（docker-compose.yml
    只掛了 ./data，沒有掛家目錄），所以必須在主機這一側自己找，
    再把內容當作 request body 上傳，而不是靠 nlm-bind-local
    去要求容器自己在檔案系統裡找到它。"""
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, ".notebooklm", "profiles", "*", "storage_state.json"),
        os.path.join(home, ".notebooklm", "browser_profile", "storage_state.json"),
    ]
    files = []
    for p in patterns:
        files.extend(glob.glob(p))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def bound_channel_ids() -> list[str]:
    """回傳目前有綁定 notebook 的 channel_id 清單——只有這些是真的
    在使用、值得續期的 channel。"""
    if not DB_PATH.exists():
        log(f"no database at {DB_PATH} yet, nothing to refresh")
        return []

    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT channel_id FROM channels WHERE notebook_id IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def rebind_channel(channel_id: str, storage_state: dict) -> str | None:
    """呼叫 nlm-login 端點，把 storage_state 的內容當作 request body
    上傳、寫回這個 channel。該端點底層一樣會先用新 cookie 真的打
    一次 API 驗證，驗證失敗就整個不寫入資料庫——所以這裡失敗
    不會弄壞現有的舊授權。回傳錯誤訊息；成功則回傳 None。"""
    url = f"http://localhost:{LOCAL_PORT}/api/channels/{channel_id}/nlm-login"
    body = json.dumps({"storage_state_json": storage_state}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        log(f"[{channel_id}] rebind ok")
        return None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        log(f"[{channel_id}] rebind failed: HTTP {e.code} {detail}")
        return f"{channel_id}: HTTP {e.code} {detail}"
    except Exception as e:
        log(f"[{channel_id}] rebind failed: {e}")
        return f"{channel_id}: {e}"


def main() -> None:
    if not refresh_cookies():
        send_admin_alert(
            "⚠️ nlm_cookie_refresh：無法從本機瀏覽器讀取 NotebookLM cookie，"
            "很可能是這台機器上的瀏覽器登入已失效。請到這台機器手動用瀏覽器"
            "重新登入一次課程帳號的 Google 帳號。"
        )
        sys.exit(1)

    channel_ids = bound_channel_ids()
    if not channel_ids:
        log("no bound channels, nothing to rebind")
        return

    path = _find_local_storage_state()
    if not path:
        send_admin_alert(
            "⚠️ nlm_cookie_refresh：cookie 讀取指令回報成功，卻找不到寫出來的"
            "storage_state.json，請檢查這台機器上的 notebooklm 設定。"
        )
        sys.exit(1)

    try:
        with open(path, "r", encoding="utf-8") as f:
            storage_state = json.load(f)
    except Exception as e:
        send_admin_alert(f"⚠️ nlm_cookie_refresh：讀取 {path} 失敗：{e}")
        sys.exit(1)

    failures = [msg for msg in (rebind_channel(cid, storage_state) for cid in channel_ids) if msg]

    if failures:
        send_admin_alert(
            "⚠️ nlm_cookie_refresh：cookie 已讀取成功，但以下 channel 回寫失敗，"
            "可能代表課程帳號的登入已經失效：\n" + "\n".join(failures)
        )
        sys.exit(1)

    log(f"done — refreshed {len(channel_ids)} channel(s)")


if __name__ == "__main__":
    main()
