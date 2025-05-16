
import pytest

from agents.extensions.file_utils import (
    FileTooLargeError,
    InstructionFileError,
    InstructionFileNotFoundError,
    InvalidFileTypeError,
    load_instructions_from_file,
)


def test_successful_read(tmp_path):
    file = tmp_path / "example.txt"
    content = "This is a test."
    file.write_text(content, encoding="utf-8")
    result = load_instructions_from_file(str(file))
    assert result == content


def test_file_not_found(tmp_path):
    file = tmp_path / "nonexistent.txt"
    with pytest.raises(InstructionFileNotFoundError):
        load_instructions_from_file(str(file))


def test_invalid_extension(tmp_path):
    file = tmp_path / "example.bin"
    file.write_text("data", encoding="utf-8")
    with pytest.raises(InvalidFileTypeError) as exc:
        load_instructions_from_file(str(file))
    assert ".bin" in str(exc.value)


def test_file_too_large(tmp_path):
    file = tmp_path / "example.txt"
    content = "a" * 20
    file.write_text(content, encoding="utf-8")
    with pytest.raises(FileTooLargeError) as exc:
        load_instructions_from_file(str(file), max_size_bytes=10)
    assert "exceeds maximum" in str(exc.value)


def test_decode_error(tmp_path):
    file = tmp_path / "example.txt"
    file.write_bytes(b'\xff\xfe\xfd')
    with pytest.raises(InstructionFileError) as exc:
        load_instructions_from_file(str(file), encoding="utf-8")
    assert "Could not decode file" in str(exc.value)
