import asyncpg
import json

class PostgreSQLSession:
    def __init__(self, session_id: str, neon_url: str):
        self.session_id = session_id
        self.__neon_url = neon_url
        self.__conn = None

    async def __connect(self):
        if not self.__conn:
            self.__conn = await asyncpg.connect(self.__neon_url)
            await self.__create_tables()

    async def __create_tables(self):
        await self.__conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await self.__conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                session_id TEXT,
                message_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

    async def add_items(self, items: list[dict])->None:
        await self.__connect()
        # Ensure session row exists
        await self.__conn.execute("""
            INSERT INTO sessions (session_id)
            VALUES ($1)
            ON CONFLICT DO NOTHING;
        """, self.session_id)

        # Insert each message
        for item in items:
            await self.__conn.execute("""
                INSERT INTO messages (session_id, message_data)
                VALUES ($1, $2);
            """, self.session_id, json.dumps(item))

    async def get_items(self) -> list[dict]:
        await self.__connect()
        rows = await self.__conn.fetch("""
            SELECT message_data FROM messages
            WHERE session_id = $1
            ORDER BY created_at ASC;
        """, self.session_id)
        return [json.loads(row['message_data']) for row in rows]
    
    async def pop_item(self) -> dict | None:
        await self.__connect()
        row = await self.__conn.fetchrow("""
            SELECT id, message_data FROM messages
            WHERE session_id = $1
            ORDER BY created_at DESC
            LIMIT 1;
        """, self.session_id)

        if row:
            await self.__conn.execute("DELETE FROM messages WHERE id = $1;", row["id"])
            return json.loads(row["message_data"])
        return None
    
    async def clear_session(self)->None:
        await self.__connect()
        await self.__conn.execute("""
            DELETE FROM messages WHERE session_id = $1;
        """, self.session_id)
