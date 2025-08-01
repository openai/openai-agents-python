from .session_abc import SessionABC


class MongoDBSession(SessionABC):
    """MongoDB-based implementation of session storage."""

    def __init__(self) -> None:
        pass