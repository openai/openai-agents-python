from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class FieldInfo:
    id: str
    name: str
    type: str
    options: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class TableInfo:
    id: str
    name: str
    primary_field_id: Optional[str]
    fields: List[FieldInfo]

    def field_by_name(self, field_name: str) -> Optional[FieldInfo]:
        for f in self.fields:
            if f.name == field_name:
                return f
        return None


@dataclass(frozen=True)
class BaseSchema:
    base_id: str
    tables: List[TableInfo]

    def table_by_name(self, table_name: str) -> Optional[TableInfo]:
        for t in self.tables:
            if t.name == table_name:
                return t
        return None


def load_schema_from_json(path: str | Path) -> BaseSchema:
    p = Path(path)
    data = json.loads(p.read_text())
    tables_raw = data["tables"] if isinstance(data, dict) else data
    tables: List[TableInfo] = []
    for t in tables_raw:
        fields = [
            FieldInfo(
                id=f["id"],
                name=f.get("name", ""),
                type=f.get("type", ""),
                options=f.get("options"),
            )
            for f in t.get("fields", [])
        ]
        tables.append(
            TableInfo(
                id=t["id"],
                name=t.get("name", ""),
                primary_field_id=t.get("primaryFieldId"),
                fields=fields,
            )
        )

    # Try to infer base_id from the filename pattern ..._appXXXX_schema.json
    base_id = p.stem.split("_")[-2] if "_" in p.stem and p.stem.endswith("schema") else ""
    return BaseSchema(base_id=base_id, tables=tables)
