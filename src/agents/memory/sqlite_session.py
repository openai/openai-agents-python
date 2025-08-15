import sqlite3
import json
from pathlib import Path

class SQLiteSession:
    def __init__(self, db_path: str = "session.db"):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self._create_table()

    def _create_table(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS session_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_input TEXT NOT NULL,
                response TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def save_interaction(self, session_id: str, user_input: str, response: str):
        # 🛠 FIX: Repeated input ko avoid karo
        last_input = self.get_last_input(session_id)
        if last_input == user_input:
            print("[INFO] Skipping repeated input.")
            return

        self.conn.execute(
            "INSERT INTO session_data (session_id, user_input, response) VALUES (?, ?, ?)",
            (session_id, user_input, response)
        )
        self.conn.commit()

    def get_last_input(self, session_id: str):
        cursor = self.conn.execute(
            "SELECT user_input FROM session_data WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,)
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def get_history(self, session_id: str):
        cursor = self.conn.execute(
            "SELECT user_input, response FROM session_data WHERE session_id = ?",
            (session_id,)
        )
        return [{"input": inp, "response": res} for inp, res in cursor.fetchall()]
