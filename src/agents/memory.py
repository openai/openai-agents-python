import abc
import json
from pathlib import Path
from typing import Any, Optional, List, Dict

class AgentMemory(abc.ABC):
    """Abstract base class for agent memory."""

    @abc.abstractmethod
    def add(self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Adds a message to the memory.

        Args:
            role: The role of the message sender (e.g., "user", "assistant").
            content: The content of the message.
            metadata: Optional metadata associated with the message.
        """
        pass

    @abc.abstractmethod
    def get_messages(self) -> List[Dict[str, Any]]:
        """Retrieves all messages from the memory.

        Returns:
            A list of messages, where each message is a dictionary
            with "role", "content", and optional "metadata" keys.
        """
        pass

    @abc.abstractmethod
    def get_last_n_messages(self, n: int) -> List[Dict[str, Any]]:
        """Retrieves the last N messages from the memory.

        Args:
            n: The number of most recent messages to retrieve.

        Returns:
            A list of the last N messages.
        """
        pass

    @abc.abstractmethod
    def clear(self) -> None:
        """Clears all messages from the memory."""
        pass

    @abc.abstractmethod
    def load(self) -> None:
        """Loads memory from a persistent store (if applicable)."""
        pass

    @abc.abstractmethod
    def save(self) -> None:
        """Saves memory to a persistent store (if applicable)."""
        pass

class InMemoryMemory(AgentMemory):
    """Stores messages in a private list in memory."""

    def __init__(self):
        self._messages: List[Dict[str, Any]] = []

    def add(self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        message = {"role": role, "content": content}
        if metadata:
            message["metadata"] = metadata
        self._messages.append(message)

    def get_messages(self) -> List[Dict[str, Any]]:
        return list(self._messages)

    def get_last_n_messages(self, n: int) -> List[Dict[str, Any]]:
        return list(self._messages[-n:])

    def clear(self) -> None:
        self._messages.clear()

    def load(self) -> None:
        """In-memory storage does not require loading."""
        # Or raise NotImplementedError("In-memory storage does not support loading.")
        pass

    def save(self) -> None:
        """In-memory storage does not require saving."""
        # Or raise NotImplementedError("In-memory storage does not support saving.")
        pass

class FileStorageMemory(AgentMemory):
    """Stores messages in a JSON file."""

    def __init__(self, file_path: Path | str):
        self.file_path = Path(file_path)
        self._messages: List[Dict[str, Any]] = []
        self.load()

    def add(self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        message = {"role": role, "content": content}
        if metadata:
            message["metadata"] = metadata
        self._messages.append(message)
        self.save() # Save after each addition

    def get_messages(self) -> List[Dict[str, Any]]:
        return list(self._messages)

    def get_last_n_messages(self, n: int) -> List[Dict[str, Any]]:
        return list(self._messages[-n:])

    def clear(self) -> None:
        self._messages.clear()
        self.save() # Save after clearing

    def load(self) -> None:
        if self.file_path.exists() and self.file_path.stat().st_size > 0:
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self._messages = json.load(f)
            except json.JSONDecodeError:
                # File is corrupted or not valid JSON, start with empty memory
                self._messages = []
            except Exception:
                # Other potential errors during file reading
                self._messages = []
        else:
            self._messages = []

    def save(self) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self._messages, f, indent=2)
        except Exception:
            # Handle potential errors during file writing,
            # e.g., permissions, disk full
            # For now, we'll let it pass, but in a real app, logging would be good.
            pass
