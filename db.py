import sqlite3
from typing import Optional

DB_PATH = "tasks.db"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    """Create tables if they don't exist."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                due_date TEXT NOT NULL,
                original_text TEXT,
                remind_before INTEGER DEFAULT 30,
                is_done INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        conn.commit()


def ensure_user(user_id: int):
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,)
        )
        conn.commit()


def add_task(user_id: int, title: str, due_date: str,
             original_text: str = "", remind_before: int = 30) -> int:
    with _conn() as conn:
        cursor = conn.execute(
            """INSERT INTO tasks (user_id, title, due_date, original_text, remind_before)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, title, due_date, original_text, remind_before)
        )
        conn.commit()
        return cursor.lastrowid


def get_task(task_id: int, user_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id)
        ).fetchone()
        return dict(row) if row else None


def get_active_tasks(user_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE user_id = ? AND is_done = 0
               ORDER BY due_date ASC""",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_done_tasks(user_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE user_id = ? AND is_done = 1
               ORDER BY due_date DESC
               LIMIT 20""",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_active_tasks() -> list[dict]:
    """Get all active tasks for all users (for rescheduling on startup)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE is_done = 0"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_done(task_id: int):
    with _conn() as conn:
        conn.execute(
            "UPDATE tasks SET is_done = 1 WHERE id = ?",
            (task_id,)
        )
        conn.commit()


def clear_done_tasks(user_id: int):
    with _conn() as conn:
        conn.execute(
            "DELETE FROM tasks WHERE user_id = ? AND is_done = 1",
            (user_id,)
        )
        conn.commit()
