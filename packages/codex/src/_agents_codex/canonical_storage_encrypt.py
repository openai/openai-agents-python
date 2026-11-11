"""Authenticated encryption for canonical SessionStore snapshots."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ._legacy_history import (
    check_legacy_history_size as _check_legacy_history_size,
    decode_legacy_item as _decode_legacy_item,
    validate_legacy_history_limits as _validate_legacy_history_limits,
)
from ._public import preserve_module_identity as _preserve_module_identity

if TYPE_CHECKING:
    from cryptography.fernet import Fernet


def _master_key_bytes(encryption_key: str) -> bytes:
    if not isinstance(encryption_key, str) or not encryption_key:
        raise ValueError("encryption_key must be nonempty text")
    try:
        decoded = base64.urlsafe_b64decode(encryption_key)
    except (ValueError, TypeError):
        decoded = b""
    return decoded if len(decoded) == 32 else encryption_key.encode("utf-8")


class EncryptedSessionStore:
    """Encrypt opaque native snapshots while preserving backend ownership.

    The backend retains its existing durability and fencing guarantees. Revision
    and write identifiers remain visible; snapshot contents use authenticated
    per-session Fernet encryption. Supply a high-entropy master key, not a
    password: HKDF does not harden passwords. Keep the key outside the backend.

    Each adapter uses one master key for canonical snapshots. Snapshot
    retention follows the backend record lifecycle. `read_legacy_history`
    applies the v0 item TTL during import.
    """

    def __init__(self, backend: SessionStore, *, encryption_key: str) -> None:
        if not isinstance(backend, SessionStore):
            raise TypeError("backend must implement SessionStore")
        self._key = _master_key_bytes(encryption_key)
        # Import the optional encryption dependency only when selecting this adapter.
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        self.backend = backend
        self._fernet_type = Fernet
        self._hashes = hashes
        self._hkdf_type = HKDF

    @staticmethod
    def read_legacy_history(
        items: Sequence[Mapping[str, Any]],
        session_id: str,
        *,
        encryption_key: str,
        ttl: int = 600,
        limit: int | None = None,
        max_items: int = 10_000,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> list[dict[str, Any]]:
        """Authenticate v0 envelopes and return unexpired items in insertion order.

        Use the original session ID, master key, and item TTL. A limit applies
        to the newest authenticated items. Import the result into a native
        Session backed by an EncryptedSessionStore. The import checks the
        source item count and the bytes of ciphertext it examines.
        """
        history: list[dict[str, Any]] = []
        try:
            from cryptography.fernet import Fernet, InvalidToken
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF

            _validate_legacy_history_limits(max_items, max_bytes)
            if limit is not None and limit <= 0:
                return []
            _check_legacy_history_size(len(items), 0, max_items, max_bytes)
            cipher: Fernet | None = None
            try:
                derived = HKDF(
                    algorithm=hashes.SHA256(),
                    length=32,
                    salt=session_id.encode("utf-8"),
                    info=b"agents.session-store.hkdf.v1",
                ).derive(_master_key_bytes(encryption_key))
                cipher = Fernet(base64.urlsafe_b64encode(derived))
            except (TypeError, ValueError):
                pass
            if cipher is None:
                raise ValueError("Invalid v0 encryption key or session ID")

            encoded_bytes = 0
            source = items if limit is None else reversed(items)
            for envelope in source:
                token = envelope.get("payload")
                if (
                    envelope.get("__enc__") != 1
                    or "kid" not in envelope
                    or "v" not in envelope
                    or not isinstance(token, str)
                    or not token.isascii()
                ):
                    continue
                encoded_bytes += len(token)
                _check_legacy_history_size(0, encoded_bytes, max_items, max_bytes)
                try:
                    item = _decode_legacy_item(cipher.decrypt(token.encode("utf-8"), ttl=ttl))
                except InvalidToken:
                    continue
                except (TypeError, ValueError, RecursionError):
                    item = None
                if item is None:
                    raise ValueError("An authenticated v0 session item is invalid")
                history.append(item)
                if limit is not None and len(history) == limit:
                    break
            if limit is not None:
                history.reverse()
            return history
        finally:
            # Exception-frame diagnostics must not retain keys, ciphertext or private history.
            session_id = encryption_key = ""
            derived = b""
            items = source = ()
            envelope = {}
            cipher = item = token = None
            history = []

    def _cipher(self, session_id: str) -> Fernet:
        derived = self._hkdf_type(
            algorithm=self._hashes.SHA256(),
            length=32,
            salt=session_id.encode("utf-8"),
            info=b"agents.canonical-session-store.hkdf.v1",
        ).derive(self._key)
        return self._fernet_type(base64.urlsafe_b64encode(derived))

    def _decrypt(self, session_id: str, stored: str) -> str:
        envelope: Any = None
        document: Any = None
        payload = b""
        text = ""
        try:
            envelope = json.loads(stored)
            if envelope["format"] != "agents.canonical.encrypted.v1":
                raise ValueError
            payload = self._cipher(session_id).decrypt(envelope["ciphertext"].encode("ascii"))
            text = payload.decode("utf-8")
            document = json.loads(text)
            if (
                document["revision"] != envelope["revision"]
                or document["write_id"] != envelope["write_id"]
            ):
                raise ValueError
            return text
        except Exception:
            pass
        finally:
            # Failed authentication must not retain data in completed frames.
            envelope = document = None
            payload = b""
            text = stored = ""
        # Raise outside the handler so raw crypto/parse exceptions are detached.
        raise RuntimeError("Encrypted canonical storage authentication failed")

    def acquire(self, session_id: str) -> str:
        return self.backend.acquire(session_id)

    def read(self, session_id: str, token: str) -> str | None:
        stored = self.backend.read(session_id, token)
        try:
            return None if stored is None else self._decrypt(session_id, stored)
        finally:
            stored = None

    def commit(
        self, session_id: str, token: str, expected_revision: int, write_id: str, payload: str
    ) -> None:
        document: Any = None
        stored: str | None = None
        previous = encrypted = ""
        try:
            document = json.loads(payload)
            if document["revision"] != expected_revision + 1 or document["write_id"] != write_id:
                raise RuntimeError("Invalid canonical revision")
            stored = self.backend.read(session_id, token)
            if stored is not None:
                previous = self._decrypt(session_id, stored)
                if json.loads(previous)["write_id"] == write_id:
                    if previous != payload:
                        raise RuntimeError("Canonical storage write identifier conflict")
                    # Reuse the accepted envelope after a lost commit acknowledgement.
                    self.backend.commit(session_id, token, expected_revision, write_id, stored)
                    return
            encrypted = json.dumps(
                {
                    "format": "agents.canonical.encrypted.v1",
                    "revision": document["revision"],
                    "write_id": write_id,
                    "ciphertext": self._cipher(session_id)
                    .encrypt(payload.encode("utf-8"))
                    .decode("ascii"),
                },
                separators=(",", ":"),
            )
            self.backend.commit(session_id, token, expected_revision, write_id, encrypted)
        finally:
            document = stored = None
            payload = previous = encrypted = ""

    def flush(self, session_id: str, token: str) -> None:
        self.backend.flush(session_id, token)

    def release(self, session_id: str, token: str) -> None:
        self.backend.release(session_id, token)


# Bind deferred annotations after the adapter exists, including leaf-first imports.
from .canonical_storage import SessionStore  # noqa: E402

_preserve_module_identity(globals(), "agents.canonical_storage")
