from __future__ import annotations

from typing import Any

from agents.items import TResponseInputItem
from agents.memory import Session, SQLiteSession


def _user(text: str) -> TResponseInputItem:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _assistant(text: str) -> TResponseInputItem:
    return {"type": "message", "role": "assistant", "content": text}


TURN_ONE: list[TResponseInputItem] = [_user("turn one user"), _assistant("turn one assistant")]
TURN_TWO: list[TResponseInputItem] = [_user("turn two user"), _assistant("turn two assistant")]
TURN_THREE: list[TResponseInputItem] = [
    _user("turn three user"),
    _assistant("turn three assistant"),
]


ALL_TURNS: list[TResponseInputItem] = [*TURN_ONE, *TURN_TWO, *TURN_THREE]


async def build_session(tmp_path: Any, *, session_id: str = "helper-session") -> Session:
    """Create a SQLite-backed session preloaded with three complete turns."""
    session = SQLiteSession(session_id, str(tmp_path / f"{session_id}.db"))
    await session.add_items(ALL_TURNS)
    return session
