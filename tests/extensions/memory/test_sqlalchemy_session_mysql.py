"""Opt-in MySQL / MariaDB integration tests for :class:`SQLAlchemySession`.

The rest of ``test_sqlalchemy_session.py`` runs against SQLite and mocked
engines, which cannot establish server-side outcomes: a compiled ``CREATE
TABLE`` says nothing about whether InnoDB accepts the index, whether a
``latin1`` database default mangles 4-byte characters, or whether a
collation pads trailing spaces. Those only show up against a real server.

These tests are therefore **opt-in** and skip unless a server is pointed at
explicitly. They need a MySQL-family async driver, which is not a project
dependency -- ``asyncmy`` is what the URLs below assume::

    uv run --with asyncmy \\
      env OPENAI_RUN_MYSQL_SESSION_TESTS=1 \\
          OPENAI_MYSQL_SESSION_URL=mysql+asyncmy://root:pw@127.0.0.1:3306 \\
      pytest tests/extensions/memory/test_sqlalchemy_session_mysql.py

A server can be had with::

    docker run -d -e MYSQL_ROOT_PASSWORD=pw -p 3306:3306 mysql:8.0

``OPENAI_MARIADB_SESSION_URL`` points at a MariaDB server the same way; each
URL is exercised independently, so setting only one is fine. The URLs are
server-level (no trailing database name) because each test creates and drops
its own database -- that keeps runs idempotent and independent of leftover
rows from an earlier run.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("sqlalchemy")  # Skip tests if SQLAlchemy is not installed

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents import TResponseInputItem
from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession

# Serial: each case creates and drops a server-side database, so parallel
# xdist workers would race on the same name space.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.serial,
    pytest.mark.skipif(
        os.environ.get("OPENAI_RUN_MYSQL_SESSION_TESTS") != "1",
        reason="Set OPENAI_RUN_MYSQL_SESSION_TESTS=1 and OPENAI_MYSQL_SESSION_URL / "
        "OPENAI_MARIADB_SESSION_URL to run MySQL-family integration tests.",
    ),
]

_SERVER_URL_ENV = {
    "mysql": "OPENAI_MYSQL_SESSION_URL",
    "mariadb": "OPENAI_MARIADB_SESSION_URL",
}


def _server_url(flavour: str) -> str:
    url = os.environ.get(_SERVER_URL_ENV[flavour])
    if not url:
        pytest.skip(f"Set {_SERVER_URL_ENV[flavour]} to run the {flavour} cases.")
    return url.rstrip("/")


def _user(content: str) -> TResponseInputItem:
    item: TResponseInputItem = {"role": "user", "content": content}
    return item


def _assistant(content: str) -> TResponseInputItem:
    item: TResponseInputItem = {"role": "assistant", "content": content}
    return item


def _contents(items: list[TResponseInputItem]) -> list[str]:
    return [str(item.get("content")) for item in items]


@pytest.fixture(params=sorted(_SERVER_URL_ENV))
def flavour(request: pytest.FixtureRequest) -> str:
    """Run every case against each configured server."""
    return str(request.param)


@pytest.fixture
async def engine(flavour: str) -> AsyncIterator[AsyncEngine]:
    """A freshly created database, dropped again afterwards.

    Created with a deliberately non-utf8mb4 default (``latin1``) so the
    session's own column-level charset is what has to carry 4-byte
    characters. A server whose default is already utf8mb4 would hide that.
    """
    server = _server_url(flavour)
    database = f"agents_it_{uuid4().hex[:12]}"

    admin = create_async_engine(f"{server}/", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(
                text(f"CREATE DATABASE {database} CHARACTER SET latin1 COLLATE latin1_swedish_ci")
            )
    finally:
        await admin.dispose()

    db_engine = create_async_engine(f"{server}/{database}")
    try:
        yield db_engine
    finally:
        await db_engine.dispose()
        admin = create_async_engine(f"{server}/", isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f"DROP DATABASE IF EXISTS {database}"))
        finally:
            await admin.dispose()


async def _column(engine: AsyncEngine, table: str, column: str) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, COLLATION_NAME "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
                ),
                {"t": table, "c": column},
            )
        ).one()


async def test_schema_is_created_on_the_server(engine: AsyncEngine) -> None:
    """Tables, the session index and the message foreign key really exist.

    Read back from ``information_schema`` rather than from the emitted DDL:
    an unbounded ``TEXT`` session id compiles fine but is rejected by InnoDB
    as a key, which is the failure this schema avoids.
    """
    session = SQLAlchemySession("schema-check", engine=engine, create_tables=True)
    await session._ensure_tables()

    data_type, length, collation = await _column(engine, "agent_sessions", "session_id")
    assert data_type == "varchar", "session_id must be bounded so it can be indexed"
    assert length is not None and length <= 191, (
        f"session_id length {length} exceeds the utf8mb4 index-prefix limit"
    )
    assert collation == "utf8mb4_bin"

    async with engine.connect() as conn:
        indexes = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_messages'"
                    )
                )
            ).all()
        }
        foreign_keys = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'agent_messages' "
                        "AND REFERENCED_TABLE_NAME IS NOT NULL"
                    )
                )
            ).all()
        }
    assert indexes, "agent_messages should carry at least one index"
    assert "agent_sessions" in foreign_keys


async def test_add_get_pop_clear_round_trip(engine: AsyncEngine) -> None:
    session = SQLAlchemySession("crud", engine=engine, create_tables=True)

    await session.add_items([_user("hello"), _assistant("hi there")])
    assert _contents(await session.get_items()) == ["hello", "hi there"]

    popped = await session.pop_item()
    assert popped is not None
    assert str(popped.get("content")) == "hi there"
    assert len(await session.get_items()) == 1

    await session.clear_session()
    assert await session.get_items() == []


async def test_four_byte_characters_survive_a_latin1_database_default(
    engine: AsyncEngine,
) -> None:
    """The column charset, not the database default, must carry the content.

    The fixture's database is ``latin1``; an emoji stored without a
    column-level utf8mb4 would be mangled or rejected rather than returned
    intact.
    """
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT @@character_set_database"))).scalar_one() == (
            "latin1"
        )

    session = SQLAlchemySession("unicode", engine=engine, create_tables=True)
    original = "你好 😀 café"
    await session.add_items([_user(original)])

    assert _contents(await session.get_items()) == [original]


async def test_case_differing_session_ids_do_not_share_history(engine: AsyncEngine) -> None:
    """``utf8mb4_bin`` keeps ids distinct that a ``_ci`` collation would merge."""
    lower = SQLAlchemySession("tenant", engine=engine, create_tables=True)
    upper = SQLAlchemySession("Tenant", engine=engine, create_tables=True)

    await lower.add_items([_user("from-lowercase")])
    await upper.add_items([_user("from-uppercase")])

    assert _contents(await lower.get_items()) == ["from-lowercase"]
    assert _contents(await upper.get_items()) == ["from-uppercase"]


async def test_trailing_space_session_id_cannot_silently_share_history(
    engine: AsyncEngine,
) -> None:
    """A PAD SPACE collation makes ``'tenant '`` and ``'tenant'`` compare equal.

    Under MySQL 8 ``utf8mb4_bin`` is PAD SPACE, so a trailing-space id would
    silently read and write another session's history. The session must
    either reject it up front or keep the two genuinely separate; silently
    merging them is the outcome this pins against.
    """
    base = SQLAlchemySession("tenant", engine=engine, create_tables=True)
    await base.add_items([_user("from-base")])

    try:
        trailing = SQLAlchemySession("tenant ", engine=engine, create_tables=True)
        await trailing.add_items([_user("from-trailing-space")])
    except ValueError as exc:
        assert "PAD SPACE" in str(exc)
        assert _contents(await base.get_items()) == ["from-base"]
        return

    # Accepted: only valid if the collation really keeps the two apart.
    assert _contents(await trailing.get_items()) == ["from-trailing-space"]
    assert _contents(await base.get_items()) == ["from-base"]


async def test_caller_managed_schema_is_usable(engine: AsyncEngine) -> None:
    """``create_tables=False`` works against tables the caller already owns."""
    await SQLAlchemySession("owned", engine=engine, create_tables=True)._ensure_tables()

    session = SQLAlchemySession("owned", engine=engine, create_tables=False)
    await session.add_items([_user("against pre-existing tables")])

    assert _contents(await session.get_items()) == ["against pre-existing tables"]
