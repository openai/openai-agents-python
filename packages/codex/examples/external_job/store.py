"""Application storage for a fixed 32 by 32 thumbnail worker."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote_from_bytes

_PRIVATE_STATE = "Use private state owned by the application outside all model-visible mounts."


def _private_file(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and info.st_mode & 0o077 == 0
        and info.st_nlink == 1
    )


class ThumbnailStore:
    """An absolute host-selected POSIX state path outside every model-visible mount.

    The host supplies an existing trusted parent directory and trusts processes
    with its effective OS identity. POSIX SQLite creates private database files
    using the directory's permissions. Keep the database, locks, sidecars, and
    receipts out of model mounts.
    """

    def __init__(self, directory: Path, tenant: str) -> None:
        if not directory.is_absolute():
            raise PermissionError(_PRIVATE_STATE)
        self.directory = directory
        self.tenant = tenant
        created = False
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            created = True
        except OSError:
            pass
        if not created:
            raise PermissionError(_PRIVATE_STATE)
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS thumbnails (
                tenant TEXT, order_id TEXT, color TEXT NOT NULL, receipt TEXT NOT NULL UNIQUE,
                image BLOB, PRIMARY KEY (tenant, order_id))""")

    def _check_directory(self) -> None:
        directory_info = None
        try:
            directory_info = self.directory.lstat()
        except OSError:
            pass
        if (
            directory_info is None
            or not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.geteuid()
            or directory_info.st_mode & 0o077
        ):
            raise PermissionError(_PRIVATE_STATE)

    def _existing_private_file(self, name: str) -> bool:
        info = None
        try:
            info = (self.directory / name).lstat()
        except FileNotFoundError:
            return False
        except OSError:
            pass
        if info is None or not _private_file(info):
            raise PermissionError(_PRIVATE_STATE)
        return True

    @contextmanager
    def private_lock(self, key: str) -> Iterator[BinaryIO]:
        """Open one host-owned process lock without following its leaf."""
        self._check_directory()
        descriptor = None
        try:
            descriptor = os.open(
                self.directory / (key + ".lock"),
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
        except OSError:
            pass
        if descriptor is None:
            raise PermissionError(_PRIVATE_STATE)
        try:
            if not _private_file(os.fstat(descriptor)):
                raise PermissionError(_PRIVATE_STATE)
            with os.fdopen(descriptor, "r+b", closefd=False) as file:
                yield file
        finally:
            os.close(descriptor)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._check_directory()
        self._existing_private_file("orders.sqlite3")
        for suffix in ("-journal", "-wal", "-shm"):
            self._existing_private_file("orders.sqlite3" + suffix)
        database = self.directory / "orders.sqlite3"
        permissions = quote_from_bytes(os.fsencode(self.directory), safe="")
        db = sqlite3.connect(database.as_uri() + "?modeof=" + permissions, timeout=3, uri=True)
        try:
            if not self._existing_private_file("orders.sqlite3"):
                raise PermissionError(_PRIVATE_STATE)
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        finally:
            db.close()

    def work(self, order_id: str) -> bool:
        """Render and publish the entire image in one authorized worker transaction."""
        with self.transaction() as db:
            row = db.execute(
                """SELECT color FROM thumbnails JOIN orders USING (tenant,order_id)
                   WHERE tenant=? AND order_id=? AND allowed=1 AND image IS NULL""",
                (self.tenant, order_id),
            ).fetchone()
            if not row:
                return False
            pixel = {"blue": bytes((0, 0, 255)), "red": bytes((255, 0, 0))}[row[0]]
            image = b"P6\n32 32\n255\n" + pixel * 32 * 32
            db.execute(
                "UPDATE thumbnails SET image=? WHERE tenant=? AND order_id=?",
                (image, self.tenant, order_id),
            )
            return True
