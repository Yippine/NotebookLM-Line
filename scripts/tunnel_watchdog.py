"""Keep a cloudflared quick tunnel alive and self-healing.

Free quick tunnels (``cloudflared tunnel --url ...``) have no uptime
guarantee: Cloudflare can silently drop the DNS registration while the
local process keeps running with no error, or the tunnel can simply exit.
This watchdog:

  1. Starts (or restarts) a quick tunnel pointing at localhost:8083.
  2. Periodically health-checks the assigned hostname.
  3. Whenever the hostname changes (first start, or after a silent-death
     restart), automatically:
       a. Updates backend/.env's WEBHOOK_BASE_URL
       b. Recreates the backend container (`docker compose up -d`) so it
          picks up the new value
       c. Pushes the new webhook URL to LINE for every bound channel via
          the Messaging API (PUT /v2/bot/channel/webhook/endpoint) — no
          manual paste into LINE Developers Console needed.

Run this once in a terminal and leave it running:
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

# Real quick-tunnel hostnames are always several hyphen-separated random
# words (e.g. "reward-clearance-answer-authorities.trycloudflare.com").
# cloudflared's non-TTY (piped) output can also print a bare placeholder
# like "api.trycloudflare.com" before the real one is assigned — require at
# least 3 segments so that decoy never matches.
_HOSTNAME_RE = re.compile(r"https://([a-z0-9]+(?:-[a-z0-9]+){2,}\.trycloudflare\.com)")


def log(msg: str) -> None:
    print(f"[watchdog] {msg}", flush=True)


def _drain(stream) -> None:
    """Keep reading a subprocess's stdout so its pipe buffer never fills up."""
    for _ in stream:
        pass


def start_tunnel() -> tuple[subprocess.Popen, str]:
    """Launch cloudflared and block until it reports its assigned hostname."""
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


# Cloudflare edge error codes meaning "reached Cloudflare fine, but it
# can't reach the local tunnel/origin" — a dead tunnel, not a dead app.
# https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/
_TUNNEL_DEAD_HTTP_CODES = {502, 521, 522, 523, 524, 530}


def is_dns_alive(hostname: str) -> bool:
    """True only if the tunnel is actually forwarding to the local app.

    A plain HTTP error from *our own app* (e.g. 403 from a bad signature)
    means the tunnel is fine. A Cloudflare edge error (e.g. 530) means the
    tunnel registration exists but isn't actually connected to anything —
    that's a dead tunnel wearing a live hostname.
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
    """Quick tunnels can take a few seconds to become reachable after
    creation. Retry before treating a freshly-started tunnel as bad —
    never touch .env/backend/LINE off a single failed check."""
    for attempt in range(1, tries + 1):
        if is_dns_alive(hostname):
            return True
        log(f"{hostname} not reachable yet (attempt {attempt}/{tries})")
        time.sleep(delay)
    return False


def start_healthy_tunnel(max_attempts: int = 5) -> tuple[subprocess.Popen, str]:
    """Start a tunnel and confirm it's actually reachable before returning it.

    Some quick-tunnel registrations never become reachable at all (DNS
    "Non-existent domain" even after the full grace period) — that's a bad
    tunnel, not a slow one. Discard it and request a brand new one rather
    than getting stuck waiting on a registration that will never resolve.
    """
    for attempt in range(1, max_attempts + 1):
        proc, hostname = start_tunnel()
        log(f"tunnel candidate: https://{hostname} (attempt {attempt}/{max_attempts})")
        if wait_until_reachable(hostname):
            return proc, hostname
        log(f"{hostname} never became reachable, discarding and requesting a fresh tunnel")
        proc.kill()
    raise RuntimeError(f"failed to get a reachable tunnel after {max_attempts} attempts")


def apply_new_hostname(hostname: str) -> None:
    url = f"https://{hostname}"
    log(f"applying new tunnel URL: {url}")
    update_env_webhook_url(url)
    restart_backend()
    sync_line_webhook_urls(url)
    log("done — .env updated, backend restarted, LINE channels synced")


def main() -> None:
    proc, hostname = start_healthy_tunnel()
    log(f"tunnel up: https://{hostname}")
    try:
        apply_new_hostname(hostname)
    except Exception:
        proc.kill()
        raise

    try:
        while True:
            time.sleep(CHECK_INTERVAL_SECONDS)
            if not is_dns_alive(hostname):
                log("tunnel appears dead, restarting cloudflared...")
                proc.kill()
                proc, hostname = start_healthy_tunnel()
                log(f"tunnel back up: https://{hostname}")
                try:
                    apply_new_hostname(hostname)
                except Exception:
                    proc.kill()
                    raise
    except KeyboardInterrupt:
        log("stopping, killing tunnel process")
        proc.kill()
        sys.exit(0)


if __name__ == "__main__":
    main()
