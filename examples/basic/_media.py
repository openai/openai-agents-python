"""Prepare known example inputs in a form stock Codex can read."""

import argparse
import base64
import subprocess
from pathlib import Path

import httpx

from agents import RunConfig


def arguments(*, remote: bool = False) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", action="store_true", help="Use stock Codex.")
    if remote:
        parser.add_argument("--file", type=Path, help="Use this local file instead of the URL.")
    args = parser.parse_args()
    if remote and args.file is not None and not args.codex:
        parser.error("--file requires --codex")
    return args


def codex_config() -> RunConfig:
    from agents.models.codex import CodexProvider

    return RunConfig(model_provider=CodexProvider())


def read_sample(url: str, file: Path | None) -> bytes:
    if file is not None:
        return file.read_bytes()
    response = httpx.get(url, follow_redirects=True, timeout=30)
    response.raise_for_status()
    return response.content


def jpeg_data_url(data: bytes) -> str:
    if not data.startswith(b"\xff\xd8\xff"):
        raise ValueError("Expected a JPEG. For offline use, pass --file with a local JPEG.")
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def pdf_text(data: bytes) -> str:
    """Read visible text, not images or page layout. No OCR."""
    try:
        result = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-nopgbrk", "-", "-"],
            input=data,
            stdout=subprocess.PIPE,
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError("Install Poppler (pdftotext) to read PDF text with --codex.") from None
    text = result.stdout.decode().strip()
    if not text:
        raise ValueError("This PDF has no extractable text.")
    return text
