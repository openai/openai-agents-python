"""Encrypted Session wrapper for secure conversation storage.

This module provides transparent encryption for session storage with automatic
expiration of old data. When TTL expires, expired items are silently skipped.

Usage::

    import os

    from agents.extensions.memory import EncryptedSession, SQLAlchemySession

    # Create underlying session (e.g. SQLAlchemySession)
    underlying_session = SQLAlchemySession.from_url(
        session_id="user-123",
        url="postgresql+asyncpg://app:secret@db.example.com/agents",
        create_tables=True,
    )

    # Load a high-entropy key provisioned through your secret manager.
    # Reuse the same key and session ID to read stored data after a restart.
    encryption_key = os.environ["SESSION_ENCRYPTION_KEY"]

    # Wrap with encryption and TTL-based expiration
    session = EncryptedSession(
        session_id="user-123",
        underlying_session=underlying_session,
        encryption_key=encryption_key,
        ttl=600,  # 10 minutes
    )

    await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Literal, TypeGuard, cast

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from typing_extensions import TypedDict

from ...items import TResponseInputItem
from ...memory.openai_responses_compaction_session import OpenAIResponsesCompactionSession
from ...memory.session import (
    OpenAIResponsesCompactionArgs,
    Session,
    SessionABC,
    _call_session_method,
    _CompactionSnapshot,
    _get_session_wrapper,
)
from ...memory.session_settings import SessionSettings, resolve_session_limit
from ...run_context import RunContextWrapper


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
    Expired records remain in the underlying store. By default, finding valid
    items may read the entire retained history. Set ``max_scan_items`` to bound
    the cumulative number of items retrieved and unwrapped per ``get_items`` call;
    overlapping backfill windows count again. This does not bound item byte size,
    backend-internal work, elapsed time, or ``pop_item`` work.
    Successful automatic compaction on native SQLite and SQLAlchemy stores reclaims a
    contiguous prefix of authenticated expired envelopes in bounded batches.

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
        max_scan_items: int | None = None,
    ):
        """
        Args:
            session_id: ID for this session
            underlying_session: The real session store (e.g. SQLiteSession, SQLAlchemySession)
            encryption_key: High-entropy master key, such as
                Fernet.generate_key().decode("ascii"), or a securely provisioned
                random secret. Keep the key secret and reuse the same key and
                session_id to read persisted data after a restart. Raw strings
                remain accepted for compatibility, but passwords and other
                low-entropy secrets are unsuitable: HKDF does not harden passwords,
                and the session ID salt does not add secret entropy.
            ttl: Token time-to-live in seconds (default 10 min)
            max_scan_items: Positive per-read item budget, or None for unlimited
                work (the default). The underlying store must honor requested
                limits. Raises RuntimeError when a complete result cannot be
                established within the budget, even if a full final window is
                actually the entire store. Increase the budget or manage retained
                history in the underlying store before retrying. When a budget is
                set, negative effective retrieval limits raise ValueError before
                reading storage; use zero to request no history.

        Raises:
            ValueError: If max_scan_items is not positive.
        """
        if max_scan_items is not None and max_scan_items <= 0:
            raise ValueError("max_scan_items must be positive or None")
        self.max_scan_items = max_scan_items
        self.session_id = session_id
        self.underlying_session = underlying_session
        self.ttl = ttl

        master = _ensure_fernet_key_bytes(encryption_key)
        self.cipher = _derive_session_fernet_key(master, session_id)
        self._kid = "hkdf-v1"
        self._ver = 1
        # One-use read budget for envelopes omitted by the latest history read.
        # This is not evidence of completeness or permission to replace history.
        self._compaction_read_overhead = 0

    def __getattr__(self, name: str) -> Any:
        # Expose compaction only when the underlying session actually supports it.
        if isinstance(self.underlying_session, OpenAIResponsesCompactionSession):
            if name == "run_compaction":
                return self._run_compaction
            if name == "_defer_compaction":
                return self._defer_encrypted_compaction
        return getattr(self.underlying_session, name)

    async def _run_compaction(
        self,
        args: OpenAIResponsesCompactionArgs | None = None,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None:
        session = cast(OpenAIResponsesCompactionSession, self.underlying_session)
        await session._run_compaction(
            args,
            wrapper=wrapper,
            read_items=lambda limit: self._read_compaction_items(limit, wrapper=wrapper),
            prepare_items=self._encrypt_items,
            read_snapshot=self._get_compaction_snapshot,
        )

    async def _defer_encrypted_compaction(
        self,
        response_id: str,
        store: bool | None = None,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None:
        session = cast(OpenAIResponsesCompactionSession, self.underlying_session)
        await session._defer_compaction(
            response_id,
            store,
            read_items=lambda limit: self._read_compaction_items(limit, wrapper=wrapper),
        )

    async def _get_compaction_snapshot(self, limit: int) -> _CompactionSnapshot | None:
        backend: Session = self.underlying_session
        if isinstance(backend, OpenAIResponsesCompactionSession):
            backend = backend.underlying_session
        reader = getattr(backend, "_get_compaction_snapshot", None)
        if reader is None:
            return None
        # Authenticate timestamps before authorizing deletion; InvalidToken alone
        # can also mean a wrong key, corruption or a future timestamp.
        expired_before = int(time.time()) - self.ttl
        cipher = self.cipher

        def is_expired(item: TResponseInputItem) -> bool:
            if not _is_encrypted_envelope(item):
                return False
            try:
                return cipher.extract_timestamp(item["payload"]) < expired_before
            except (InvalidToken, TypeError):
                return False

        raw: _CompactionSnapshot | None = await reader(limit, prune_prefix=is_expired)
        if raw is None:
            return None
        items: list[TResponseInputItem] = []
        positions: list[int] = []
        retained_boundary = 0
        for index, encrypted_item in enumerate(raw.items):
            item = self._unwrap(encrypted_item)
            if item is not None:
                items.append(item)
                positions.append(index)
            elif not is_expired(encrypted_item):
                # An unreadable envelope is not necessarily expired. Keep it and
                # everything before it outside the eligible replacement suffix.
                retained_boundary = index + 1
                items.clear()
                positions.clear()

        async def replace_suffix(start: int, output: list[TResponseInputItem]) -> bool:
            # Observed expired rows may be part of the selected raw suffix.
            # The transaction separately reclaims the authenticated expired prefix.
            raw_start = retained_boundary if start == 0 else positions[start]
            return await raw.replace_suffix(raw_start, self._encrypt_items(output))

        return _CompactionSnapshot(items, raw.complete and retained_boundary == 0, replace_suffix)

    async def _read_compaction_items(
        self, limit: int | None, *, wrapper: RunContextWrapper[Any] | None = None
    ) -> tuple[list[TResponseInputItem], bool]:
        if limit is None:
            # A default-limited policy read must not discard overhead already
            # observed by a larger model-history read.
            overhead = self._compaction_read_overhead
            items = await _call_session_method(
                self.get_items, wrapper=_get_session_wrapper(self, wrapper)
            )
            self._compaction_read_overhead = max(overhead, self._compaction_read_overhead)
            return items, False
        # Reuse only overhead already observed during ordinary history retrieval.
        # Consume it before I/O; current raw completeness and plaintext visibility
        # still decide whether compaction can replace the stored history.
        raw_limit = limit + self._compaction_read_overhead
        self._compaction_read_overhead = 0
        items = cast(
            list[TResponseInputItem],
            await _call_session_method(
                self.underlying_session.get_items,
                raw_limit,
                wrapper=_get_session_wrapper(self.underlying_session, wrapper),
            ),
        )
        if len(items) >= raw_limit:
            return [], False
        return self._unwrap_valid_items(items), True

    @property
    def session_settings(self) -> SessionSettings | None:
        """Get session settings from the underlying session."""
        return self.underlying_session.session_settings

    @session_settings.setter
    def session_settings(self, value: SessionSettings | None) -> None:
        """Set session settings on the underlying session."""
        self.underlying_session.session_settings = value

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

    def _unwrap_valid_items(
        self, encrypted_items: list[TResponseInputItem]
    ) -> list[TResponseInputItem]:
        valid_items: list[TResponseInputItem] = []
        for enc in encrypted_items:
            item = self._unwrap(enc)
            if item is not None:
                valid_items.append(item)
        return valid_items

    async def get_items(
        self,
        limit: int | None = None,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> list[TResponseInputItem]:
        self._compaction_read_overhead = 0
        wrapper = _get_session_wrapper(self.underlying_session, wrapper)
        effective_limit = resolve_session_limit(limit, self.session_settings)
        remaining = self.max_scan_items
        if remaining is not None and effective_limit is not None and effective_limit < 0:
            raise ValueError("limit must be non-negative when max_scan_items is set")
        positive_limit = effective_limit is not None and effective_limit > 0
        if positive_limit or (remaining is not None and effective_limit is None):
            window = effective_limit if positive_limit else remaining
            assert window is not None
            if remaining is not None:
                window = min(window, remaining)
            while True:
                encrypted_items = cast(
                    list[TResponseInputItem],
                    await _call_session_method(
                        self.underlying_session.get_items,
                        window,
                        wrapper=wrapper,
                    ),
                )
                valid_items = self._unwrap_valid_items(encrypted_items)
                self._compaction_read_overhead = len(encrypted_items) - len(valid_items)
                if (
                    positive_limit
                    and effective_limit is not None
                    and len(valid_items) >= effective_limit
                ):
                    return valid_items[-effective_limit:]
                if len(encrypted_items) < window:
                    return valid_items
                next_window = window * 2
                if remaining is not None:
                    remaining -= len(encrypted_items)
                    next_window = min(next_window, remaining)
                    if next_window <= window:
                        raise RuntimeError(
                            "EncryptedSession max_scan_items exhausted before a complete "
                            "history result could be established; increase max_scan_items "
                            "or reduce retained history."
                        )
                window = next_window

        encrypted_items = cast(
            list[TResponseInputItem],
            await _call_session_method(
                self.underlying_session.get_items,
                limit,
                wrapper=wrapper,
            ),
        )
        valid_items = self._unwrap_valid_items(encrypted_items)
        self._compaction_read_overhead = len(encrypted_items) - len(valid_items)
        return valid_items

    def _encrypt_items(self, items: list[TResponseInputItem]) -> list[TResponseInputItem]:
        return cast(list[TResponseInputItem], [self._wrap(item) for item in items])

    async def add_items(
        self,
        items: list[TResponseInputItem],
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None:
        wrapper = _get_session_wrapper(self.underlying_session, wrapper)
        await _call_session_method(
            self.underlying_session.add_items,
            self._encrypt_items(items),
            wrapper=wrapper,
        )

    async def pop_item(
        self,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> TResponseInputItem | None:
        wrapper = _get_session_wrapper(self.underlying_session, wrapper)
        while True:
            enc = await _call_session_method(
                self.underlying_session.pop_item,
                wrapper=wrapper,
            )
            if not enc:
                return None
            item = self._unwrap(enc)
            if item is not None:
                return item

    async def clear_session(
        self,
        *,
        wrapper: RunContextWrapper[Any] | None = None,
    ) -> None:
        wrapper = _get_session_wrapper(self.underlying_session, wrapper)
        await _call_session_method(
            self.underlying_session.clear_session,
            wrapper=wrapper,
        )
