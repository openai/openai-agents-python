# agents/agent_outputs.py
"""
Shared, strictly‑typed output schemas for Agents SDK.
Phase α only covers ProfileBuilder; add more as you refactor other agents.
"""

from typing import List, Union
from pydantic import BaseModel


class ProfileFieldOut(BaseModel):
    """One field‑value pair collected by the profile‑builder agent."""
    field_name: str
    value: Union[str, List[str]]


class ClarificationOut(BaseModel):
    """Prompt asking the user to supply missing info."""
    prompt: str
