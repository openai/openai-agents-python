from ..exceptions import AgentsException


class STTWebsocketConnectionError(AgentsException):
    """Exception raised when the STT websocket connection fails."""

    def __init__(self, message: str):
        self.message = message
