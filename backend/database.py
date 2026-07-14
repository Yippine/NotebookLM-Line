import os
import aiosqlite
from config import settings

DB = settings.db_path


async def init_db():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY,
                channel_secret TEXT NOT NULL,
                channel_access_token TEXT NOT NULL,
                nlm_auth_json_encrypted TEXT,
                notebook_id TEXT,
                expires_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                code TEXT PRIMARY KEY,
                student_name TEXT DEFAULT '',
                used INTEGER DEFAULT 0,
                channel_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_conversations (
                channel_id TEXT NOT NULL,
                line_user_id TEXT NOT NULL,
                conversation_id TEXT,
                drive_folder_id TEXT,
                drive_folder_link TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (channel_id, line_user_id)
            )
        """)
        await db.commit()

        # Migrate: add columns if missing (for existing DBs)
        for table, col, col_def in [
            ("channels", "expires_at", "DATETIME"),
            ("invite_codes", "student_name", "TEXT DEFAULT ''"),
            ("invite_codes", "channel_id", "TEXT"),
            ("user_conversations", "drive_folder_id", "TEXT"),
            ("user_conversations", "drive_folder_link", "TEXT"),
        ]:
            cur = await db.execute(f"PRAGMA table_info({table})")
            cols = [r[1] for r in await cur.fetchall()]
            if col not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
                await db.commit()
