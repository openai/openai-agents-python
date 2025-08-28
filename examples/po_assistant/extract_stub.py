from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class POHeader:
    po_number: Optional[str]
    company_name: Optional[str]
    currency: Optional[str]


@dataclass
class POLine:
    raw_description: str
    sku: Optional[str]
    vendor_part: Optional[str]
    quantity: Optional[int]
    unit_price: Optional[float]


@dataclass
class POExtract:
    header: POHeader
    lines: List[POLine]


def extract_from_pdf_bytes(_: bytes) -> POExtract:
    # Placeholder: a deterministic stub to enable the reconciliation flow during scaffolding.
    header = POHeader(po_number=None, company_name=None, currency=None)
    lines = [
        POLine(raw_description="Item A", sku=None, vendor_part=None, quantity=1, unit_price=None),
        POLine(raw_description="Item B", sku=None, vendor_part=None, quantity=2, unit_price=None),
    ]
    return POExtract(header=header, lines=lines)
