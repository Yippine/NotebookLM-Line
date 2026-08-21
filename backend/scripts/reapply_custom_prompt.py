"""一次性腳本：把最新的 ``RESTRICTED_TOPIC_CUSTOM_PROMPT`` 重新推送給已經
綁定過 NotebookLM 的所有 channel。

背景：``configure_restricted_topic_guard`` 只會在使用者「第一次綁定」
（``bind_nlm``）或「切換筆記本」（``select_notebook``）時被呼叫一次，
之後這個自訂人設 prompt 是儲存在 NotebookLM 那一端、跟著筆記本走的
設定，不會因為我們重新部署後端、甚至重啟容器就自動更新——修改
``services/nlm_service.py`` 裡的 ``RESTRICTED_TOPIC_CUSTOM_PROMPT`` 常數
只會影響*之後新綁定*的筆記本，已經綁定過的舊筆記本仍然停留在修改前的
舊版本人設，除非手動重新呼叫一次 ``configure_restricted_topic_guard``。
這支腳本就是用來補這一步：找出所有已綁定（``nlm_auth_json_encrypted``
與 ``notebook_id`` 都有值）的 channel，逐一重新套用目前程式碼裡的最新
版本 prompt。

用法（在 backend 容器內執行，因為需要 notebooklm-py 等相依套件）：

    docker compose exec backend python3 scripts/reapply_custom_prompt.py

預設會處理資料庫裡所有已綁定的 channel；也可以用 ``--channel-id`` 只
處理其中一個：

    docker compose exec backend python3 scripts/reapply_custom_prompt.py --channel-id <channel_id>
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite

from database import DB
from services import nlm_service
from services.crypto_service import decrypt


async def _load_bound_channels(channel_id: str | None):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        if channel_id:
            cur = await db.execute(
                "SELECT channel_id, nlm_auth_json_encrypted, notebook_id FROM channels "
                "WHERE channel_id=?",
                (channel_id,),
            )
        else:
            cur = await db.execute(
                "SELECT channel_id, nlm_auth_json_encrypted, notebook_id FROM channels "
                "WHERE nlm_auth_json_encrypted IS NOT NULL AND notebook_id IS NOT NULL"
            )
        return await cur.fetchall()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel-id", default=None,
        help="只重新套用這一個 channel；不加的話會處理所有已綁定的 channel",
    )
    args = parser.parse_args()

    rows = await _load_bound_channels(args.channel_id)
    if not rows:
        print("找不到任何已綁定 NotebookLM 的 channel。")
        return

    print(f"共 {len(rows)} 個已綁定的 channel，開始重新套用最新版人設 prompt：")
    ok, failed = 0, []
    for row in rows:
        channel_id = row["channel_id"]
        notebook_id = row["notebook_id"]
        if not row["nlm_auth_json_encrypted"] or not notebook_id:
            print(f"  - {channel_id}：尚未完整綁定，略過。")
            continue
        storage_state = decrypt(row["nlm_auth_json_encrypted"])
        try:
            await nlm_service.configure_restricted_topic_guard(storage_state, notebook_id)
            print(f"  ✅ {channel_id}（筆記本 {notebook_id}）已更新。")
            ok += 1
        except Exception as e:
            print(f"  ⚠️ {channel_id}（筆記本 {notebook_id}）更新失敗：{e}")
            failed.append(channel_id)

    print(f"\n完成：{ok} 個成功，{len(failed)} 個失敗。")
    if failed:
        print("失敗的 channel：" + "、".join(failed))


if __name__ == "__main__":
    asyncio.run(main())
