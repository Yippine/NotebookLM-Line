"""偵測到後端標記某個 channel 的 NotebookLM 登入已失效時，立刻從
本機瀏覽器讀取已登入的 cookie、回寫給所有已綁定的 channel，藉此
降低「TrueExpiry」（整個 session 死掉）時需要人工重新登入的頻率。

背景：NotebookLM 沒有官方 API，只能靠瀏覽器 session cookie
（storage_state.json）存取。這組 cookie 有兩種失效方式：

  - Rotation（正常換發）：已經全自動，每次開 client 套件都會自動
    跟 Google 換新值，不需要這支腳本處理。
  - TrueExpiry（整個 session 死掉）：目前唯一解法是人工重新登入。

這支腳本不能消除 TrueExpiry——桌面瀏覽器的登入本身還是會被
Google 風控清掉，只是比伺服器端純 cookie 的存活期長很多（經驗上
是天到週的量級）。它做的事情，是把「多久要人工重登一次」的頻率
降低，而不是把人工完全消除：只要這台機器上的瀏覽器本身還維持
登入，這支腳本就能在偵測到失效的當下把新鮮的 cookie 續回資料庫。

偵測是事件驅動的，不是排程驅動的：後端（見 nlm_service.py 的
ask_question / check_all_channels_health）一偵測到認證失敗，就會
立刻把該 channel 的 nlm_health_status 標成 expired，寫進跟後端
共用的同一份 SQLite 資料庫。這支腳本每次執行時，第一步永遠是先
查一次這個欄位（any_channel_expired()）——沒有任何 channel 被標成
expired，就直接安靜結束，不做任何昂貴或敏感的事（不讀瀏覽器
cookie、不打 admin API、不發告警）。正因為「沒事做」的那一輪成本
低到可以忽略，這支腳本可以排得很頻繁（例如每 1-2 分鐘一次），
讓「偵測到掛了」與「真的刷新」之間的延遲從過去最長半小時，縮短到
接近這個排程間隔本身，不必再靠拉長單次排程的間隔去「猜」多久會被
發現。

只有續期失敗時才會發 LINE 通知給管理員——正常運作時完全靜默，
不會每次執行都發一則「一切正常」的訊息來洗版。由於現在排程跑得
頻繁很多，同一個尚未解決的失敗原因會加上 30 分鐘的告警冷卻
（見 _ALERT_COOLDOWN_SECONDS），避免同一個問題被連續灌成一長串
重複通知。

讀到的 storage_state.json 內容一旦上傳給後端，本機這份含有
cookie 的檔案就沒有用處了——會覆寫內容後再刪除（shred，而不是
單純 os.remove），不讓它在磁碟上多留存。下一輪執行會直接從
Firefox 重新產生一份新的，不依賴這份殘留的舊檔。

前提條件：
  - 這支腳本要在跟後端同一台機器上執行（本機讀瀏覽器 cookie、
    直接讀後端共用的 SQLite 資料庫、再打 localhost 的 admin API）
  - 該機器上的 Firefox 必須已經用共用帳號登入過 NotebookLM，並且
    保持登入——不要登出、不要清 cookie、不要換帳號登入。這件事
    沒有技術手段能強制保障，純粹要靠自己維持這個習慣
  - 需要額外安裝：pip install "notebooklm-py[cookies]"

用法（手動測試一次；若目前沒有任何 channel 被標成 expired，
會直接安靜結束，看不到任何刷新動作，這是正常的）：
    python scripts/nlm_cookie_refresh.py

排定成 Windows 排程工作（每 1 分鐘檢查一次是否需要刷新；沒事的
那幾輪成本可以忽略，排密一點只是讓真正需要刷新時反應更快）：
    schtasks /create /tn "NLM Cookie Refresh" /sc minute /mo 1 ^
        /tr "python C:\\path\\to\\scripts\\nlm_cookie_refresh.py" /rl highest

排定成 Ubuntu/Linux 的 cron（同樣每 1 分鐘檢查一次；完整部署步驟見
docs/ubuntu-cookie-refresh-runbook.md）：
    * * * * * cd /path/to/repo && python3 scripts/nlm_cookie_refresh.py >> /var/log/nlm_cookie_refresh.log 2>&1
"""

import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / "backend" / ".env"
DB_PATH = ROOT / "data" / "data.db"
ALERT_STATE_PATH = ROOT / "data" / "nlm_cookie_refresh_alert_state.json"
LOCAL_PORT = 8083
BROWSER = "firefox"
SUBPROCESS_TIMEOUT_SECONDS = 60

# 同一種失敗原因（用下面各個 reason_key 區分），在這個秒數之內只
# 告警一次。排程現在跑得很頻繁（分鐘級），沒有這道冷卻的話，同一個
# 尚未解決的問題會被連續灌成一長串重複的 LINE 通知。
_ALERT_COOLDOWN_SECONDS = 30 * 60


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


def _load_alert_state() -> dict:
    if not ALERT_STATE_PATH.exists():
        return {}
    try:
        return json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_alert_state(state: dict) -> None:
    ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def send_admin_alert_with_cooldown(reason_key: str, message: str) -> None:
    """跟 send_admin_alert 一樣發告警，但同一個 reason_key 在
    _ALERT_COOLDOWN_SECONDS 之內只發一次，避免排程改密之後，
    同一個尚未解決的問題被連續灌成一長串重複通知。"""
    state = _load_alert_state()
    last_sent = state.get(reason_key, 0)
    now = time.time()
    if now - last_sent < _ALERT_COOLDOWN_SECONDS:
        log(f"skip alert for {reason_key!r}: still within cooldown")
        return

    state[reason_key] = now
    _save_alert_state(state)
    send_admin_alert(message)


def _clear_alert_state() -> None:
    """成功刷新後清掉所有冷卻紀錄——下次如果又壞掉，即使是同一個
    reason_key，也能立刻告警，不會被上一次故障留下的冷卻視窗擋住。"""
    if ALERT_STATE_PATH.exists():
        try:
            ALERT_STATE_PATH.unlink()
        except Exception as e:
            log(f"failed to clear alert state: {e}")


def any_channel_expired() -> bool:
    """回傳目前是否有任何已綁定 notebook 的 channel 被後端標記為
    nlm_health_status='expired'。

    這是把這支腳本從「不管死活、固定每隔一段時間都刷新一次」改成
    「偵測到真的掛了才刷新」的關鍵閘門：後端在 ask_question() 實際
    問問題撞到認證失敗時（或 check_all_channels_health() 排程巡檢
    時）就會立刻把這個欄位標成 expired，寫進跟這支腳本共用的同一份
    SQLite 資料庫。這裡只要用很低的成本（純本機讀檔，不需要開瀏覽器
    也不需要打任何網路 API）頻繁確認一次這個欄位，就能讓「偵測到
    掛了」到「真的重新刷新 cookie」之間的延遲，從過去固定的排程
    間隔（最長可能等到半小時），縮短到接近這支腳本本身的排程間隔。
    """
    if not DB_PATH.exists():
        return False
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT 1 FROM channels WHERE notebook_id IS NOT NULL "
            "AND nlm_health_status='expired' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row is not None


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


def _shred_file(path: str) -> None:
    """覆寫檔案內容後再刪除，而不是單純 os.remove。

    單純刪除只會移除檔案系統的索引項目，內容本身在磁碟上通常還
    救得回來；覆寫過一次再刪，才是真的把這組 cookie 內容清乾淨，
    不讓它在本機硬碟上多逗留一秒。這裡只盡力而為——覆寫/刪除
    失敗只記 log，不影響這次執行的主要結果，也不重新拋出例外。"""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.write(os.urandom(size))
            f.flush()
            os.fsync(f.fileno())
        os.remove(path)
    except Exception as e:
        log(f"failed to shred {path}: {e}")


def main() -> None:
    # 這道閘門是這支腳本「偵測到掛了才刷新」的核心：沒有任何 channel
    # 被後端標記為 expired，代表 cookie 目前是活的，不需要付出讀瀏覽器
    # cookie、打 admin API 這些成本——直接安靜結束，讓排程可以排得
    # 很密（例如每分鐘），也不會有多餘的動作或告警。
    if not any_channel_expired():
        log("no channel currently marked expired, skip refresh")
        return

    log("detected an expired channel, refreshing cookies now")

    if not refresh_cookies():
        send_admin_alert_with_cooldown(
            "browser_cookie_read_failed",
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
        send_admin_alert_with_cooldown(
            "storage_state_missing",
            "⚠️ nlm_cookie_refresh：cookie 讀取指令回報成功，卻找不到寫出來的"
            "storage_state.json，請檢查這台機器上的 notebooklm 設定。"
        )
        sys.exit(1)

    try:
        with open(path, "r", encoding="utf-8") as f:
            storage_state = json.load(f)
    except Exception as e:
        _shred_file(path)
        send_admin_alert_with_cooldown("storage_state_read_failed", f"⚠️ nlm_cookie_refresh：讀取 {path} 失敗：{e}")
        sys.exit(1)

    # 內容已經讀進記憶體、之後只需要 storage_state 這個變數——磁碟上
    # 那份含有 cookie 的檔案已經沒有用處，不論接下來 rebind 成功或
    # 失敗都先清掉，不留著。
    _shred_file(path)

    failures = [msg for msg in (rebind_channel(cid, storage_state) for cid in channel_ids) if msg]

    if failures:
        send_admin_alert_with_cooldown(
            "rebind_failed",
            "⚠️ nlm_cookie_refresh：cookie 已讀取成功，但以下 channel 回寫失敗，"
            "可能代表課程帳號的登入已經失效：\n" + "\n".join(failures)
        )
        sys.exit(1)

    # 這一輪確實刷新成功了——清掉任何殘留的告警冷卻紀錄，下次如果
    # 又壞掉（即使是同一種失敗原因），也能立刻告警，不會被這次的
    # 冷卻視窗擋住。
    _clear_alert_state()
    log(f"done — refreshed {len(channel_ids)} channel(s)")


if __name__ == "__main__":
    main()
