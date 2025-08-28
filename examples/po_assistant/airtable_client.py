from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests


@dataclass
class AirtableConfig:
    base_id: str
    pat: str


class AirtableClient:
    def __init__(self, config: AirtableConfig) -> None:
        self.config = config
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.config.pat}",
                "Content-Type": "application/json",
            }
        )

    def list_records(
        self, table_name: str, fields: Optional[Iterable[str]] = None, page_size: int = 100
    ) -> List[Dict[str, Any]]:
        url = f"https://api.airtable.com/v0/{self.config.base_id}/{table_name}"
        params: Dict[str, Any] = {"pageSize": page_size}
        if fields:
            params["fields[]"] = list(fields)
        records: List[Dict[str, Any]] = []
        offset: Optional[str] = None
        while True:
            if offset:
                params["offset"] = offset
            resp = self._session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break
        return records


def config_from_env() -> AirtableConfig:
    base_id = os.getenv("AIRTABLE_BASE_ID", "appIQpYvYVDlVtAPS")
    pat = os.getenv("AIRTABLE_PAT", "")
    if not pat:
        raise RuntimeError("Missing AIRTABLE_PAT env var.")
    return AirtableConfig(base_id=base_id, pat=pat)
