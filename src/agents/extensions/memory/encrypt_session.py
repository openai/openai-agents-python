"""Encrypted Session wrapper for secure conversation storage.

This module provides transparent encryption for session storage with automatic
expiration of old data. When TTL expires, expired items are silently skipped.

Usage::

    from agents.extensions.memory import EncryptedSession, SQLAlchemySession

    # Create underlying session (e.g. SQLAlchemySession)
    underlying_session = SQLAlchemySession.from_url(
        session_id="user-123",
        url="postgresql+asyncpg://app:secret@db.example.com/agents",
        create_tables=True,
    )

    # Wrap with encryption and TTL-based expiration
    session = EncryptedSession(
        session_id="user-123",
        underlying_session=underlying_session,
        encryption_key="your-encryption-key",
        ttl=600,  # 10 minutes
    )

    await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from typing_extensions import Literal, TypedDict, TypeGuard

from ...items import TResponseInputItem
from ...memory.session import SessionABC


class EncryptedEnvelope(TypedDict):
    """TypedDict for encrypted message envelopes stored in the underlying session."""

    __enc__: Literal[1]
    v: int
    kid: str
    payload: str


def _ensure_fernet_key_bytes(master_key: str) -> bytes:
    """
    Accept either a Fernet key (urlsafe-b64, 32 bytes after decode) or a raw string.
    Returns raw bytes suitable for HKDF input.
    """
    if not master_key:
        raise ValueError("encryption_key not set; required for EncryptedSession.")
    try:
        key_bytes = base64.urlsafe_b64decode(master_key)
        if len(key_bytes) == 32:
            return key_bytes
    except Exception:
        pass
    return master_key.encode("utf-8")


def _derive_session_fernet_key(master_key_bytes: bytes, session_id: str) -> Fernet:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=session_id.encode("utf-8"),
        info=b"agents.session-store.hkdf.v1",
    )
    derived = hkdf.derive(master_key_bytes)
    return Fernet(base64.urlsafe_b64encode(derived))


def _to_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _from_json_bytes(data: bytes) -> Any:
    return json.loads(data.decode("utf-8"))


def _is_encrypted_envelope(item: object) -> TypeGuard[EncryptedEnvelope]:
    """Type guard to check if an item is an encrypted envelope."""
    return (
        isinstance(item, dict)
        and item.get("__enc__") == 1
        and "payload" in item
        and "kid" in item
        and "v" in item
    )


class EncryptedSession(SessionABC):
    """Encrypted wrapper for Session implementations with TTL-based expiration.

    This class wraps any SessionABC implementation to provide transparent
    encryption/decryption of stored items using Fernet encryption with
    per-session key derivation and automatic expiration of old data.

    When items expire (exceed TTL), they are silently skipped during retrieval.

    Note: Expired tokens are rejected based on the system clock of the application server.
    To avoid valid tokens being rejected due to clock drift, ensure all servers in
    your environment are synchronized using NTP.
    """

    def __init__(
        self,
        session_id: str,
        underlying_session: SessionABC,
        encryption_key: str,
        ttl: int = 600,
    ):
        """
        Args:
            session_id: ID for this session
            underlying_session: The real session store (e.g. SQLiteSession, SQLAlchemySession)
            encryption_key: Master key (Fernet key or raw secret)
            ttl: Token time-to-live in seconds (default 10 min)
        """
        self.session_id = session_id
        self.underlying_session = underlying_session
        self.ttl = ttl

        master = _ensure_fernet_key_bytes(encryption_key)
        self.cipher = _derive_session_fernet_key(master, session_id)
        self._kid = "hkdf-v1"
        self._ver = 1

    def __getattr__(self, name):
        return getattr(self.underlying_session, name)

    def _wrap(self, item: TResponseInputItem) -> EncryptedEnvelope:
        if isinstance(item, dict):
            payload = item
        elif hasattr(item, "model_dump"):
            payload = item.model_dump()
        elif hasattr(item, "__dict__"):
            payload = item.__dict__
        else:
            payload = dict(item)

        token = self.cipher.encrypt(_to_json_bytes(payload)).decode("utf-8")
        return {"__enc__": 1, "v": self._ver, "kid": self._kid, "payload": token}

    def _unwrap(self, item: TResponseInputItem | EncryptedEnvelope) -> TResponseInputItem | None:
        if not _is_encrypted_envelope(item):
            return cast(TResponseInputItem, item)

        try:
            token = item["payload"].encode("utf-8")
            plaintext = self.cipher.decrypt(token, ttl=self.ttl)
            return cast(TResponseInputItem, _from_json_bytes(plaintext))
        except (InvalidToken, KeyError):
            return None

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        encrypted_items = await self.underlying_session.get_items(limit)
        valid_items: list[TResponseInputItem] = []
        for enc in encrypted_items:
            item = self._unwrap(enc)
            if item is not None:
                valid_items.append(item)
        return valid_items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        wrapped: list[EncryptedEnvelope] = [self._wrap(it) for it in items]
        await self.underlying_session.add_items(cast(list[TResponseInputItem], wrapped))

    async def pop_item(self) -> TResponseInputItem | None:
        while True:
            enc = await self.underlying_session.pop_item()
            if not enc:
                return None
            item = self._unwrap(enc)
            if item is not None:
                return item

    async def clear_session(self) -> None:
        await self.underlying_session.clear_session()
