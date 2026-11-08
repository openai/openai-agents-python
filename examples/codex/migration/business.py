"""Typed application data shared by the read-only v0 and native examples."""

from dataclasses import dataclass, field

from pydantic import BaseModel


class Invoice(BaseModel):
    invoice_id: str
    amount_cents: int
    status: str


@dataclass
class Application:
    # The host supplies the invoice this user can read.
    invoice: Invoice = field(
        default_factory=lambda: Invoice(invoice_id="INV-42", amount_cents=4200, status="paid")
    )


def read_invoice(app: Application, invoice_id: str) -> Invoice:
    if invoice_id != app.invoice.invoice_id:
        raise PermissionError("Invoice is outside the application's scope.")
    return app.invoice


PROMPT = "Look up invoice INV-42 and return its ID, amount in cents, and status."
