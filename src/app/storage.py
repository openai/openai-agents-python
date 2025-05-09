# src/app/storage.py
import os
from abc import ABC, abstractmethod

# ENV var to pick backend: "bubble" (default) or "supabase"
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "bubble").lower()

class StorageBackend(ABC):
    @abstractmethod
    async def save_profile_field(self, task_id, user_id, field_name, field_value, created_at):
        ...

    @abstractmethod
    async def send_chat_message(self, task_id, user_id, message, created_at):
        ...

# will be replaced below
_storage: StorageBackend

# src/app/storage.py (continued, bubble section)
from app.util.webhook import send_webhook

class BubbleStorage(StorageBackend):
    def __init__(self):
        self.profile_url = os.getenv("PROFILE_WEBHOOK_URL")
        self.chat_url    = os.getenv("CLARIFICATION_WEBHOOK_URL")

    async def save_profile_field(self, task_id, user_id, field_name, field_value, created_at):
        payload = {
            "task_id": task_id,
            "user_id": user_id,
            "agent_type": "profilebuilder",
            "message_type": "profile_partial",
            "message_content": {field_name: field_value},
            "created_at": created_at,
        }
        await send_webhook(self.profile_url, payload)

    async def send_chat_message(self, task_id, user_id, message, created_at):
        payload = {
            "task_id": task_id,
            "user_id": user_id,
            "agent_type": "profilebuilder",
            "message_type": "text",
            "message_content": message,
            "created_at": created_at,
        }
        await send_webhook(self.chat_url, payload)

# src/app/storage.py (continued, supabase section)
from datetime import datetime
from supabase import create_client

class SupabaseStorage(StorageBackend):
    def __init__(self):
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        self.sb = create_client(url, key)

    async def save_profile_field(self, task_id, user_id, field_name, field_value, created_at):
        # Build base payload
        payload = {
            "task_id": task_id,
            "user_id": user_id,
            "updated_at": created_at,
            field_name: field_value
        }
        # Upsert into profiles table
        await self.sb.table("profiles").upsert(payload).execute()

    async def send_chat_message(self, task_id, user_id, message, created_at):
        # (optional) if you want to log chat messages
        await self.sb.table("chat_messages").insert({
            "task_id": task_id,
            "user_id": user_id,
            "content": message,
            "created_at": created_at,
        }).execute()


# src/app/storage.py (continued)
if STORAGE_BACKEND == "supabase":
    _storage = SupabaseStorage()
else:
    _storage = BubbleStorage()

# export the single instance
get_storage = lambda: _storage
