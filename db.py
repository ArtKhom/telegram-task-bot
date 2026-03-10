import os
import aiosqlite
from typing import Optional

# Railway Volume: mount path /data in Railway settings → data persists across redeploys
DB_PATH = os.getenv("DB_PATH", "/data/tasks.db")


async def init():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                due_date TEXT NOT NULL,
                category TEXT DEFAULT 'personal',
                original_text TEXT,
                remind_before INTEGER DEFAULT 30,
                is_done INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await conn.commit()
        # Migration: add category column if missing
        try:
            await conn.execute("SELECT category FROM tasks LIMIT 1")
        except Exception:
            await conn.execute("ALTER TABLE tasks ADD COLUMN category TEXT DEFAULT 'personal'")
            await conn.commit()


async def ensure_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        await conn.commit()


async def add_task(
    user_id: int,
    title: str,
    due_date: str,
    category: str = "personal",
    original_text: str = "",
    remind_before: int = 30,
) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            """INSERT INTO tasks (user_id, title, due_date, category, original_text, remind_before)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, title, due_date, category, original_text, remind_before),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_task(task_id: int, user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_tasks(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND is_done = 0 ORDER BY due_date ASC",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_done_tasks(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND is_done = 1 ORDER BY due_date DESC LIMIT 20",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_all_active_tasks() -> list:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM tasks WHERE is_done = 0")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_all_tasks_for_user(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY is_done ASC, due_date ASC",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def mark_done(task_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE tasks SET is_done = 1 WHERE id = ?", (task_id,))
        await conn.commit()


async def mark_undone(task_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE tasks SET is_done = 0 WHERE id = ?", (task_id,))
        await conn.commit()


async def delete_task(task_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        )
        await conn.commit()


async def update_task_category(task_id: int, user_id: int, category: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE tasks SET category = ? WHERE id = ? AND user_id = ?",
            (category, task_id, user_id),
        )
        await conn.commit()


async def clear_done_tasks(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "DELETE FROM tasks WHERE user_id = ? AND is_done = 1", (user_id,)
        )
        await conn.commit()
