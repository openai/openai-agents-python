"""
Helper utilities for file-based operations, e.g. loading instruction text files.
"""

from collections.abc import Sequence
from pathlib import Path


class InstructionFileError(Exception):
    """Base exception for load_instructions_from_file errors."""

class InstructionFileNotFoundError(InstructionFileError):
    """Raised when the file does not exist or is not a file."""

class InvalidFileTypeError(InstructionFileError):
    """Raised when the file extension is not allowed."""

class FileTooLargeError(InstructionFileError):
    """Raised when the file size exceeds the maximum allowed."""

def load_instructions_from_file(
    path: str,
    encoding: str = "utf-8",
    allowed_extensions: Sequence[str] = (".txt", ".md"),
    max_size_bytes: int = 1 * 1024 * 1024,
) -> str:
    """Load a text file with strict validations and return its contents.

    Args:
        path: Path to the instruction file.
        encoding: File encoding (defaults to 'utf-8').
        allowed_extensions: Tuple of allowed file extensions.
        max_size_bytes: Maximum allowed file size in bytes.

    Returns:
        The file contents as a string.

    Raises:
        InstructionFileNotFoundError: if the file does not exist or is not a file.
        InvalidFileTypeError: if the file extension is not in allowed_extensions.
        FileTooLargeError: if the file size exceeds max_size_bytes.
        InstructionFileError: for IO or decoding errors.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise InstructionFileNotFoundError(f"File not found or is not a file: {file_path}")
    if file_path.suffix.lower() not in allowed_extensions:
        raise InvalidFileTypeError(
            f"Invalid file extension {file_path.suffix!r}, allowed: {allowed_extensions}"
        )
    size = file_path.stat().st_size
    if size > max_size_bytes:
        raise FileTooLargeError(
            f"File size {size} exceeds maximum {max_size_bytes} bytes"
        )
    try:
        return file_path.read_text(encoding=encoding)
    except UnicodeDecodeError as e:
        raise InstructionFileError(f"Could not decode file {file_path}: {e}") from e
    except OSError as e:
        raise InstructionFileError(f"Error reading file {file_path}: {e}") from e
