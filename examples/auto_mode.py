"""Utilities for running examples in automated mode.

When ``EXAMPLES_INTERACTIVE_MODE=auto`` is set, these helpers provide
deterministic inputs and confirmations so examples can run without manual
interaction. The helpers are intentionally lightweight to avoid adding
dependencies to example code.
"""

from __future__ import annotations

import os


def is_auto_mode() -> bool:
    """Return True when examples should bypass interactive prompts."""
    return os.environ.get("EXAMPLES_INTERACTIVE_MODE", "").lower() == "auto"


def input_with_fallback(prompt: str, fallback: str) -> str:
    """Return the fallback text in auto mode, otherwise defer to input()."""
    if is_auto_mode():
        print(f"[auto-input] {prompt.strip()} -> {fallback}")
        return fallback
    return input(prompt)


def confirm_with_fallback(prompt: str, default: bool = True) -> bool:
    """Return default in auto mode; otherwise ask the user."""
    if is_auto_mode():
        choice = "yes" if default else "no"
        print(f"[auto-confirm] {prompt.strip()} -> {choice}")
        return default

    answer = input(prompt).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}
