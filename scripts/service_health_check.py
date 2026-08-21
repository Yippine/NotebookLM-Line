"""定期檢查 Docker 服務與對外 Tunnel 是否還活著，死掉才通知管理員。

這支腳本取代的是原本的 tunnel_watchdog.py 在「Windows + cloudflared
匿名 quick tunnel」這套組合裡扮演的角色——但職責完全不同，刻意沒有
沿用同一個檔名，避免讓人誤以為兩者是同一套邏輯的延伸：

    現在對外曝露服務用的是**具名**的 Cloudflare Tunnel（綁定固定的
    網域，例如 ``https://line-bot.yourdomain.com``），不是 cloudflared
    的匿名 quick tunnel——具名 tunnel 的網址是固定的，不會像匿名
    quick tunnel 那樣三不五時就換成一個新的隨機網址、也不受匿名
    申請的限流影響。原本 tunnel_watchdog.py 大部分的邏輯（偵測網址
    變化、改寫 .env、重啟後端、把新網址同步給 LINE）都是為了應付
    「網址會變」這件事才存在的——網址不會變了，這些邏輯也就沒有
    存在的理由。

    剩下唯一還需要人盯著的，只有「Docker 服務、對外 Tunnel 有沒有
    意外掛掉」。掛了也不是這支腳本能自己修好的（Docker Desktop
    當掉、cloudflared 服務掛掉，都需要人到主機前手動處理），所以
    這支腳本**只做通知、不做任何自動修復**，跟 tunnel_watchdog.py
    的定位完全不同。

只在「健康狀態改變」時才發通知（從正常變異常、或從異常恢復正常），
不會每次執行都重複發送同一則告警——這是刻意的設計：以前匿名
quick tunnel 三不五時死掉又活過來、加上 LINE 的每月推播訊息額度是
有限的，若排成短間隔的排程、又每次都重複告警，很容易把額度用光。

用法（排成 Windows 排程工作，例如每 10 分鐘一次）：
    schtasks /create /tn "NLM Service Health Check" /sc minute /mo 10 ^
        /tr "python C:\\path\\to\\scripts\\service_health_check.py" /rl highest
"""

import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / "backend" / ".env"
STATE_PATH = ROOT / "data" / "service_health_state.json"
REQUEST_TIMEOUT_SECONDS = 10
REQUIRED_CONTAINERS = ("nlm-line-backend", "nlm-line-frontend")

_CHECK_LABELS = {
    "docker": "Docker 服務",
    "tunnel": "對外連線（Cloudflare Tunnel）",
}


def log(msg: str) -> None:
    print(f"[health-check] {msg}", flush=True)


def _read_env_var(name: str) -> str:
    """最陽春的 .env 行讀取器，跟 nlm_cookie_refresh.py 用同一招。"""
    if not ENV_PATH.exists():
        return ""
    match = re.search(rf"(?m)^{name}=(.*)$", ENV_PATH.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else ""


def check_docker() -> str | None:
    """回傳 None 表示正常，否則回傳給人看的錯誤說明。"""
    try:
        result = subprocess.run(
            [
                "docker", "ps",
                "--filter", "name=nlm-line-backend",
                "--filter", "name=nlm-line-frontend",
                "--format", "{{.Names}}\t{{.Status}}",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return f"docker ps 執行失敗（Docker Desktop 可能沒在跑）：{e}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        return f"docker ps 回傳非 0（Docker Desktop 可能沒在跑）：{detail}"

    up_names = {
        line.split("\t")[0]
        for line in result.stdout.strip().splitlines()
        if "\t" in line and "Up" in line
    }
    missing = set(REQUIRED_CONTAINERS) - up_names
    if missing:
        return f"以下容器沒有在正常執行：{', '.join(sorted(missing))}"
    return None


def check_tunnel() -> str | None:
    """檢查 WEBHOOK_BASE_URL 對外是否還連得上。

    刻意檢查的是這個公開網址，而不是本機的 localhost:8083——
    Cloudflare Tunnel 是 cloudflared 服務自己管理的，跟後端容器是否
    健康是兩件互相獨立的事，只測本機連得上，測不出 tunnel 本身是否
    掛掉。"""
    base_url = _read_env_var("WEBHOOK_BASE_URL")
    if not base_url:
        return "backend/.env 裡沒有 WEBHOOK_BASE_URL，無法檢查"
    try:
        resp = urllib.request.urlopen(base_url, timeout=REQUEST_TIMEOUT_SECONDS)
        status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        return f"{base_url} 連不上：{e}"
    if not (200 <= status < 400):
        return f"{base_url} 回應異常狀態碼（{status}）"
    return None


def send_admin_alert(message: str) -> None:
    """盡力而為地推送 LINE 訊息給管理員。若管理員告警未設定則不做任何事。"""
    admin_user_id = _read_env_var("ADMIN_LINE_USER_ID")
    access_token = _read_env_var("ADMIN_ALERT_ACCESS_TOKEN")
    if not admin_user_id or not access_token:
        log("ADMIN_LINE_USER_ID / ADMIN_ALERT_ACCESS_TOKEN 未設定，略過告警")
        return

    body = json.dumps(
        {"to": admin_user_id, "messages": [{"type": "text", "text": message}]}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"發送管理員告警失敗：{e}")


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def main() -> None:
    checks = {"docker": check_docker(), "tunnel": check_tunnel()}
    state = _load_state()

    for name, error in checks.items():
        label = _CHECK_LABELS[name]
        was_healthy = state.get(name, True)  # 第一次執行時預設「本來是正常的」
        is_healthy = error is None

        if is_healthy and not was_healthy:
            log(f"{name} 已恢復正常")
            send_admin_alert(f"✅ {label}已恢復正常。")
        elif not is_healthy and was_healthy:
            log(f"{name} 異常：{error}")
            send_admin_alert(
                f"⚠️ {label}異常：{error}\n"
                "請盡快到主機前處理——這支腳本只負責通知，不會自動修復。"
            )
        elif is_healthy:
            log(f"{name} 正常")
        else:
            log(f"{name} 持續異常（已經通知過，不重複發送）：{error}")

        state[name] = is_healthy

    _save_state(state)


if __name__ == "__main__":
    main()
