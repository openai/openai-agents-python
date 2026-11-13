"""Idle LocalStorage capture does not relax the other persistent boundaries."""

from types import SimpleNamespace

import pytest

from agents import CustomStorage, LocalStorage, Session
from tests.native_storage_fixtures import MemorySessionStore


def owner(storage):
    return SimpleNamespace(
        _realtime=None,
        _active=None,
        _queued_roots=[],
        _storage=storage,
        _repair_receipts_incomplete=False,
        _history_ttl_seconds=None,
    )


def test_idle_local_capture_can_reach_native_export(tmp_path):
    Session._check_portable_capture(
        owner(LocalStorage(tmp_path / "profile")), allow_idle_storage=True
    )


def test_local_manual_and_fork_capture_keep_the_existing_rejection(tmp_path):
    with pytest.raises(ValueError, match="Persistent storage"):
        Session._check_portable_capture(owner(LocalStorage(tmp_path / "profile")))


def test_idle_custom_capture_can_reach_native_export():
    Session._check_portable_capture(
        owner(CustomStorage(MemorySessionStore())), allow_idle_storage=True
    )


def test_encrypted_capture_requires_original_backend():
    current = owner(CustomStorage(MemorySessionStore()))
    current._history_ttl_seconds = 0
    with pytest.raises(ValueError, match="original storage backend"):
        Session._check_portable_capture(current, allow_idle_storage=True)


def test_local_capture_still_rejects_queued_work(tmp_path):
    current = owner(LocalStorage(tmp_path / "profile"))
    current._queued_roots.append(object())
    with pytest.raises(RuntimeError, match="queued work"):
        Session._check_portable_capture(current, allow_idle_storage=True)


def test_local_capture_still_rejects_realtime(tmp_path):
    current = owner(LocalStorage(tmp_path / "profile"))
    current._realtime = object()
    with pytest.raises(ValueError, match="Realtime"):
        Session._check_portable_capture(current, allow_idle_storage=True)
