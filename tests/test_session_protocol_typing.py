from agents import Session, SQLiteSession


def _accept_session(session: Session) -> None:
    """Static typing helper: mypy should accept concrete sessions here."""


def test_sqlite_session_satisfies_session_protocol() -> None:
    session = SQLiteSession("session_protocol_test")

    _accept_session(session)

    assert isinstance(session, Session)
