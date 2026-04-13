import abc
from typing import Literal

Environment = Literal["mac", "windows", "ubuntu", "browser"]
Button = Literal["left", "right", "wheel", "back", "forward"]


class Computer(abc.ABC):
    """A computer implemented with sync operations.

    Subclasses provide the local runtime behind `ComputerTool`. Mouse action methods may
    also accept a keyword-only `keys` argument to receive held modifier keys when the
    driver supports them.
    """

    @property
    def environment(self) -> Environment | None:
        """Return preview tool metadata when the preview computer payload is required."""
        return None

    @property
    def dimensions(self) -> tuple[int, int] | None:
        """Return preview display dimensions when the preview computer payload is required."""
        return None

    @abc.abstractmethod
    def screenshot(self) -> str:
        """Return a base64-encoded PNG screenshot of the current display."""
        pass

    @abc.abstractmethod
    def click(self, x: int, y: int, button: Button) -> None:
        """Click `button` at the given `(x, y)` screen coordinates."""
        pass

    @abc.abstractmethod
    def double_click(self, x: int, y: int) -> None:
        """Double-click at the given `(x, y)` screen coordinates."""
        pass

    @abc.abstractmethod
    def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        """Scroll at `(x, y)` by `(scroll_x, scroll_y)` units."""
        pass

    @abc.abstractmethod
    def type(self, text: str) -> None:
        """Type `text` into the currently focused target."""
        pass

    @abc.abstractmethod
    def wait(self) -> None:
        """Wait until the computer is ready for the next action."""
        pass

    @abc.abstractmethod
    def move(self, x: int, y: int) -> None:
        """Move the mouse cursor to the given `(x, y)` screen coordinates."""
        pass

    @abc.abstractmethod
    def keypress(self, keys: list[str]) -> None:
        """Press the provided keys, such as `["ctrl", "c"]`."""
        pass

    @abc.abstractmethod
    def drag(self, path: list[tuple[int, int]]) -> None:
        """Click-and-drag the mouse along the given sequence of `(x, y)` waypoints."""
        pass


class AsyncComputer(abc.ABC):
    """A computer implemented with async operations.

    Subclasses provide the local runtime behind `ComputerTool`. Mouse action methods may
    also accept a keyword-only `keys` argument to receive held modifier keys when the
    driver supports them.
    """

    @property
    def environment(self) -> Environment | None:
        """Return preview tool metadata when the preview computer payload is required."""
        return None

    @property
    def dimensions(self) -> tuple[int, int] | None:
        """Return preview display dimensions when the preview computer payload is required."""
        return None

    @abc.abstractmethod
    async def screenshot(self) -> str:
        """Return a base64-encoded PNG screenshot of the current display."""
        pass

    @abc.abstractmethod
    async def click(self, x: int, y: int, button: Button) -> None:
        """Click `button` at the given `(x, y)` screen coordinates."""
        pass

    @abc.abstractmethod
    async def double_click(self, x: int, y: int) -> None:
        """Double-click at the given `(x, y)` screen coordinates."""
        pass

    @abc.abstractmethod
    async def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        """Scroll at `(x, y)` by `(scroll_x, scroll_y)` units."""
        pass

    @abc.abstractmethod
    async def type(self, text: str) -> None:
        """Type `text` into the currently focused target."""
        pass

    @abc.abstractmethod
    async def wait(self) -> None:
        """Wait until the computer is ready for the next action."""
        pass

    @abc.abstractmethod
    async def move(self, x: int, y: int) -> None:
        """Move the mouse cursor to the given `(x, y)` screen coordinates."""
        pass

    @abc.abstractmethod
    async def keypress(self, keys: list[str]) -> None:
        """Press the provided keys, such as `["ctrl", "c"]`."""
        pass

    @abc.abstractmethod
    async def drag(self, path: list[tuple[int, int]]) -> None:
        """Click-and-drag the mouse along the given sequence of `(x, y)` waypoints."""
        pass
