"""Fake model for testing purposes."""
from __future__ import annotations


class FakeModel:
    """A fake model for testing purposes."""
    model: str = "gpt-4o-mini"

    def __init__(self, initial_output=None, **kwargs):
        self.initial_output = initial_output
        self.last_turn_args = kwargs