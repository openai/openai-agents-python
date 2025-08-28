from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class CandidateModel(BaseModel):
    id: str
    name: str
    confidence: float


class LineCandidateModel(BaseModel):
    raw_description: str
    quantity: Optional[int] = None
    item_candidates: List[CandidateModel]
    available_qty: Optional[int] = None


class SyncResponse(BaseModel):
    company_candidates: List[CandidateModel]
    lines: List[LineCandidateModel]


class SyncRequest(BaseModel):
    # For now, allow either a base64-encoded PDF or plain text stub.
    po_bytes_base64: Optional[str] = None
    text_stub: Optional[str] = None
