from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


def _default_alias_path() -> Path:
    # repo_root/examples/po_assistant -> go two levels up to repo root
    return Path(__file__).resolve().parents[2] / "data" / "alias_memory.json"


ALIAS_PATH: Path = _default_alias_path()


class AliasPair(BaseModel):
    alias: str = Field(..., min_length=1)
    canonical: str = Field(..., min_length=1)


class AliasMemory(BaseModel):
    companies: Dict[str, str] = Field(default_factory=dict)
    items: Dict[str, str] = Field(default_factory=dict)


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_alias_memory(path: Optional[Path] = None) -> AliasMemory:
    p = path or ALIAS_PATH
    if not p.exists():
        return AliasMemory()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return AliasMemory(**data)
    except Exception:
        # Corrupt or invalid file → reset to empty memory.
        return AliasMemory()


def save_alias_memory(mem: AliasMemory, path: Optional[Path] = None) -> None:
    p = path or ALIAS_PATH
    _ensure_dir(p)
    p.write_text(mem.model_dump_json(indent=2), encoding="utf-8")


def add_alias_entries(
    companies: Optional[List[AliasPair]] = None,
    items: Optional[List[AliasPair]] = None,
    path: Optional[Path] = None,
) -> AliasMemory:
    mem = load_alias_memory(path)
    if companies:
        for pair in companies:
            mem.companies[pair.alias.strip()] = pair.canonical.strip()
    if items:
        for pair in items:
            mem.items[pair.alias.strip()] = pair.canonical.strip()
    save_alias_memory(mem, path)
    return mem


def clear_alias_memory(path: Optional[Path] = None) -> None:
    save_alias_memory(AliasMemory(), path)


def resolve_company_alias(name: Optional[str], path: Optional[Path] = None) -> Optional[str]:
    if not name:
        return name
    mem = load_alias_memory(path)
    # Exact key match first.
    if name in mem.companies:
        return mem.companies[name]
    # Case-insensitive and normalized matching.
    norm = name.strip().lower()
    for k, v in mem.companies.items():
        if k.strip().lower() == norm:
            return v
    return name


def resolve_item_alias(text: Optional[str], path: Optional[Path] = None) -> Optional[str]:
    if not text:
        return text
    mem = load_alias_memory(path)
    if text in mem.items:
        return mem.items[text]
    norm = text.strip().lower()
    for k, v in mem.items.items():
        if k.strip().lower() == norm:
            return v
    return text


# Test helper to override path during tests
def _set_alias_path_for_tests(test_path: Path) -> None:
    global ALIAS_PATH
    ALIAS_PATH = test_path
