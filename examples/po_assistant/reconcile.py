from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from .extract_stub import POExtract, POLine
from .matching import MatchCandidate, exact_or_best
from .airtable_client import AirtableClient, config_from_env


@dataclass
class Candidate:
    id: str
    name: str
    confidence: float


@dataclass
class LineReconciliation:
    line: POLine
    item_candidates: List[Candidate]
    available_qty: Optional[int]


@dataclass
class POReconciliation:
    company_candidates: List[Candidate]
    lines: List[LineReconciliation]


def _list_table_items(client: AirtableClient, table: str, label_field: str) -> List[Tuple[str, str]]:
    records = client.list_records(table)
    out: List[Tuple[str, str]] = []
    for r in records:
        rid = r.get("id", "")
        fields = r.get("fields", {})
        label = fields.get(label_field) or r.get("id")
        if isinstance(label, list) and label:
            # If Airtable returns array for primary field in lookups, pick first string.
            label = label[0]
        if isinstance(label, str):
            out.append((rid, label))
    return out


def reconcile(extract: POExtract) -> POReconciliation:
    client = config_from_env() and AirtableClient(config_from_env())
    # Company candidates from Clients table (by Client Name).
    client_items = _list_table_items(client, table="Clients", label_field="Client Name")
    company_mc: List[MatchCandidate] = exact_or_best(extract.header.company_name, client_items)
    company_candidates = [Candidate(id=m.id, name=m.label, confidence=m.confidence) for m in company_mc]

    # Item candidates from Product Options by Name (fallback to Product Code if present).
    item_items = _list_table_items(client, table="Product Options", label_field="Name")
    lines: List[LineReconciliation] = []
    for ln in extract.lines:
        query = ln.sku or ln.vendor_part or ln.raw_description
        item_mc = exact_or_best(query, item_items)
        item_cands = [Candidate(id=m.id, name=m.label, confidence=m.confidence) for m in item_mc]
        # TODO: fetch stock for top candidate(s) via lookups if needed; placeholder 0/None.
        lines.append(LineReconciliation(line=ln, item_candidates=item_cands, available_qty=None))
    return POReconciliation(company_candidates=company_candidates, lines=lines)


