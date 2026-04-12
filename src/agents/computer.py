import abc
from typing import Literal

Environment = Literal["mac", "windows", "ubuntu", "browser"]
Button = Literal["left", "right", "wheel", "back", "forward"]


class Computer(abc.ABC):
    """A computer implemented with sync operations. The Computer interface abstracts the
    operations needed to control a computer or browser."""

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
        """Take a screenshot and return the base64-encoded image data."""
        pass

    @abc.abstractmethod
    def click(self, x: int, y: int, button: Button) -> None:
        """Click a mouse button at the given (x, y) screen coordinates."""
        pass

    @abc.abstractmethod
    def double_click(self, x: int, y: int) -> None:
        """Double-click at the given (x, y) screen coordinates."""
        pass

    @abc.abstractmethod
    def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        """Scroll at the given (x, y) coordinates by (scroll_x, scroll_y) units."""
        pass

    @abc.abstractmethod
    def type(self, text: str) -> None:
        """Type the given text at the current cursor position."""
        pass

    @abc.abstractmethod
    def wait(self) -> None:
        """Wait for the computer to be ready for the next action."""
        pass

    @abc.abstractmethod
    def move(self, x: int, y: int) -> None:
        """Move the mouse cursor to the given (x, y) screen coordinates."""
        pass

    @abc.abstractmethod
    def keypress(self, keys: list[str]) -> None:
        """Press one or more keys simultaneously (e.g. ``["ctrl", "c"]``)."""
        pass

    @abc.abstractmethod
    def drag(self, path: list[tuple[int, int]]) -> None:
        """Click-and-drag the mouse along the sequence of (x, y) waypoints."""
        pass


class AsyncComputer(abc.ABC):
    """A computer implemented with async operations. The Computer interface abstracts the
    operations needed to control a computer or browser."""

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
        """Take a screenshot and return the base64-encoded image data."""
        pass

    @abc.abstractmethod
    async def click(self, x: int, y: int, button: Button) -> None:
        """Click a mouse button at the given (x, y) screen coordinates."""
        pass

    @abc.abstractmethod
    async def double_click(self, x: int, y: int) -> None:
        """Double-click at the given (x, y) screen coordinates."""
        pass

    @abc.abstractmethod
    async def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        """Scroll at the given (x, y) coordinates by (scroll_x, scroll_y) units."""
        pass

    @abc.abstractmethod
    async def type(self, text: str) -> None:
        """Type the given text at the current cursor position."""
        pass

    @abc.abstractmethod
    async def wait(self) -> None:
        """Wait for the computer to be ready for the next action."""
        pass

    @abc.abstractmethod
    async def move(self, x: int, y: int) -> None:
        """Move the mouse cursor to the given (x, y) screen coordinates."""
        pass

    @abc.abstractmethod
    async def keypress(self, keys: list[str]) -> None:
        """Press one or more keys simultaneously (e.g. ``["ctrl", "c"]``)."""
        pass

    @abc.abstractmethod
    async def drag(self, path: list[tuple[int, int]]) -> None:
        """Click-and-drag the mouse along the sequence of (x, y) waypoints."""
        pass
