from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class PlanLineRequest(BaseModel):
    product_option_id: str
    requested_qty: int = Field(ge=1)
    # Optional user overrides; if None, planner computes from stock.
    reserve_now: Optional[int] = None


class PlanRequest(BaseModel):
    idempotency_key: str
    po_number: Optional[str] = None
    client_id: str
    lines: List[PlanLineRequest]


class PlanLineComputed(BaseModel):
    product_option_id: str
    requested_qty: int
    available_qty: Optional[int] = None
    reserve_now: int
    backorder_qty: int


class PurchaseOrderCreate(BaseModel):
    table: str = "Purchase Orders"
    fields: Dict[str, object]


class PlanResult(BaseModel):
    idempotency_key: str
    purchase_order: PurchaseOrderCreate
    computed_lines: List[PlanLineComputed]
    notes: Optional[str] = None
