"""一次性清理腳本：刪除同一家廠商在覆蓋機制修好之前累積的重複舊來源。

背景：``services/nlm_service.py`` 裡 ``replace_sources_for_notebook`` 原本
用完整檔名逐字比對來判斷「是否為同一份要覆蓋的檔案」，但檔名慣例是
``{廠商}{日期}``，同一家廠商每天上傳的檔名都不同，逐字比對永遠對不上，
導致舊檔案從未被刪除、一路往上疊加。這個 bug 已經修好（改用廠商名稱
比對），但**修好之前**累積的重複檔案不會自動清掉，需要手動跑這支腳本
補一次。之後不會再需要——正常覆蓋流程會自己維持每家廠商只有一份最新
的來源。

用法（在 backend 容器內執行，因為需要 notebooklm-py 等相依套件）：

    docker compose exec backend python3 scripts/cleanup_duplicate_sources.py --channel-id <channel_id>

預設是 dry-run，只列出「會刪除什麼」，不會真的動手。確認沒問題後加上
``--apply`` 才會真的執行刪除：

    docker compose exec backend python3 scripts/cleanup_duplicate_sources.py --channel-id <channel_id> --apply

只清理檔名符合「廠商+8位數日期」慣例、且能明確排出新舊順序的來源；
舊式 ``{廠商}_...`` 命名（沒有日期、無法判斷新舊）一律跳過不動，避免
誤刪唯一一份還沒有日期版本可取代的資料。
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite

from database import DB
from services import nlm_service
from services.crypto_service import decrypt

_TRAILING_DATE_RE = re.compile(r"(\d{8})$")


def _vendor_and_date(title: str) -> tuple[str, str] | None:
    """回傳 (廠商名稱, 8位數日期字串)。實際命名慣例是
    ``廠商_YYYYMMDD``（底線分隔），也相容不加底線的
    ``廠商YYYYMMDD``。底線後面不是純 8 位數日期的舊式命名
    （例如 ``廠商_型錄``，日期不明、無法排出新舊順序）則回傳
    None，交由呼叫端跳過不處理。"""
    stem = Path(title).stem
    vendor, sep, rest = stem.partition("_")
    if sep:
        if re.fullmatch(r"\d{8}", rest) and vendor:
            return vendor, rest
        return None

    match = _TRAILING_DATE_RE.search(stem)
    if not match:
        return None
    vendor = stem[: match.start()]
    if not vendor:
        return None
    return vendor, match.group(1)


async def _load_channel(channel_id: str):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT nlm_auth_json_encrypted, notebook_id FROM channels WHERE channel_id=?",
            (channel_id,),
        )
        return await cur.fetchone()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-id", required=True, help="要清理的 channel_id")
    parser.add_argument(
        "--apply", action="store_true",
        help="真的執行刪除；不加這個旗標只會列出會刪除什麼（dry-run）",
    )
    args = parser.parse_args()

    row = await _load_channel(args.channel_id)
    if not row or not row["nlm_auth_json_encrypted"] or not row["notebook_id"]:
        print(f"⚠️ channel {args.channel_id} 尚未綁定 NotebookLM，無法清理。")
        return

    notebook_id = row["notebook_id"]
    storage_state = decrypt(row["nlm_auth_json_encrypted"])

    async def _list(client):
        return await client.sources.list(notebook_id)

    sources = await nlm_service._run_with_auth(storage_state, _list)
    print(f"筆記本 {notebook_id} 目前共有 {len(sources)} 份來源。")

    groups: dict[str, list[tuple[str, object]]] = {}
    skipped = []
    for source in sources:
        parsed = _vendor_and_date(source.title)
        if parsed is None:
            skipped.append(source.title)
            continue
        vendor, date_str = parsed
        groups.setdefault(vendor, []).append((date_str, source))

    to_delete = []
    for vendor, items in groups.items():
        if len(items) <= 1:
            continue
        items.sort(key=lambda pair: pair[0], reverse=True)
        keep_date, keep_source = items[0]
        print(f"\n廠商「{vendor}」有 {len(items)} 份，保留最新的 {keep_date}（{keep_source.title}）：")
        for date_str, source in items[1:]:
            print(f"  - 刪除 {date_str}（{source.title}）")
            to_delete.append(source)

    if skipped:
        print(f"\n以下 {len(skipped)} 份檔名不符合「廠商+日期」慣例，略過不處理：")
        for title in skipped:
            print(f"  - {title}")

    if not to_delete:
        print("\n沒有需要清理的重複來源。")
        return

    if not args.apply:
        print(f"\n[dry-run] 共會刪除 {len(to_delete)} 份重複來源。加上 --apply 才會真的執行。")
        return

    async def _delete_all(client):
        for source in to_delete:
            await client.sources.delete(notebook_id, source.id)

    await nlm_service._run_with_auth(storage_state, _delete_all)
    nlm_service._invalidate_sources_cache(notebook_id)
    print(f"\n✅ 已刪除 {len(to_delete)} 份重複來源。")


if __name__ == "__main__":
    asyncio.run(main())
