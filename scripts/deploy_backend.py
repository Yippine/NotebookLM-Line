"""安全地重新建置並部署 backend 容器。

背景：backend 容器目前是同步阻塞式處理每個問題——收到訊息後會
一直等到 NotebookLM 查詢完成（實測過最久要 5 分半）才回覆，這段
期間如果容器被重啟（不管是手動部署、還是這台開發機睡眠喚醒），
整個查詢會被無聲打斷，使用者不會收到任何錯誤訊息，也不會有任何
告警——這是 2026-08-11/12 這兩天實際發生過至少兩次的真實事故。

這支腳本在真正執行 `docker compose build/up` 之前，會先確認最近
一次收到的問題是否已經有完成訊號（生成完成的 log、已送出的
reply、或錯誤）——沒有的話就等，而不是直接動手重啟。

用法：
    python scripts/deploy_backend.py
"""

import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTAINER = "nlm-line-backend"
DB_PATH = ROOT / "data" / "data.db"
LOCAL_PORT = 8083

# 觀察到過最久的查詢是 5 分半（多廠商完整比較表）；抓寬一點的上限，
# 但不要真的無限等——如果等到這麼久還沒完成，比較可能是查詢本身
# 卡死了，繼續等下去也沒有意義，不如直接部署。
MAX_WAIT_SECONDS = 600
POLL_INTERVAL_SECONDS = 10
# 只看最近幾分鐘的 log 判斷「有沒有問題還在進行中」——比用固定行數
# （--tail N）可靠，不受 log 產生速度影響。
LOOKBACK_WINDOW = "15m"

_Q_RE = re.compile(r"\(user\) Q:|\(group\) Q:")
_DONE_RE = re.compile(r"raw answer \(for vendor-split debugging\)|message/reply|\] Error:")


def log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def _docker_logs() -> str:
    result = subprocess.run(
        ["docker", "logs", "--since", LOOKBACK_WINDOW, CONTAINER],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout + result.stderr


def _has_in_flight_query(logs: str) -> bool:
    """檢查最近一次收到的問題，是否還沒有對應的完成訊號。"""
    lines = logs.splitlines()
    last_q_idx = None
    for i, line in enumerate(lines):
        if _Q_RE.search(line):
            last_q_idx = i
    if last_q_idx is None:
        return False
    return not any(_DONE_RE.search(line) for line in lines[last_q_idx:])


def wait_until_idle() -> None:
    waited = 0
    while waited < MAX_WAIT_SECONDS:
        if not _has_in_flight_query(_docker_logs()):
            return
        log(
            f"有查詢還在進行中，{POLL_INTERVAL_SECONDS} 秒後重新檢查"
            f"（已等待 {waited}s／上限 {MAX_WAIT_SECONDS}s）"
        )
        time.sleep(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS
    log(
        f"等待超過 {MAX_WAIT_SECONDS} 秒仍有查詢在進行中，放棄等待、直接部署"
        "——請自行確認這是真的卡住了，而不是查詢本身異常地久。"
    )


def run(cmd: list[str]) -> None:
    log(f"執行：{' '.join(cmd)}")
    subprocess.run(cmd, cwd=ROOT, check=True)


def refresh_guards() -> None:
    """為每個已綁定 NotebookLM 的 channel 重新推送人設指令
    （RESTRICTED_TOPIC_CUSTOM_PROMPT 若有更新，部署後要重推才生效）。"""
    if not DB_PATH.exists():
        log("找不到資料庫，略過 refresh-guard")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        channel_ids = [
            row[0]
            for row in conn.execute(
                "SELECT channel_id FROM channels WHERE notebook_id IS NOT NULL"
            )
        ]
    finally:
        conn.close()

    for channel_id in channel_ids:
        url = f"http://localhost:{LOCAL_PORT}/api/channels/{channel_id}/refresh-guard"
        req = urllib.request.Request(url, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            log(f"[{channel_id}] refresh-guard ok")
        except urllib.error.URLError as e:
            log(f"[{channel_id}] refresh-guard failed: {e}")


def main() -> None:
    log("檢查是否有查詢還在進行中……")
    wait_until_idle()

    run(["docker", "compose", "build", "backend"])
    run(["docker", "compose", "up", "-d", "backend"])

    log("等待後端啟動……")
    time.sleep(3)

    refresh_guards()
    log("部署完成。")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        log(f"部署失敗：{e}")
        sys.exit(1)
