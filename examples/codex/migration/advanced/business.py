"""Application data and business rules shared by three explicit SDK ports."""

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel


class Invoice(BaseModel):
    invoice_id: str
    amount_cents: int
    status: str


class PolicyReview(BaseModel):
    category: str
    recommendation: str


class SupportAnswer(BaseModel):
    invoice_id: str
    amount_cents: int
    case_id: str
    label: str


@dataclass(frozen=True)
class Policy:
    text: str = "Paid invoices go to billing support. A review grants no write permission."


@dataclass
class Application:
    """The host supplies this tenant scope; a model cannot choose it."""

    tenant_id: str = "tenant-a"
    invoice: Invoice = field(
        default_factory=lambda: Invoice(invoice_id="INV-42", amount_cents=4200, status="paid")
    )
    case_writes: list[str] = field(default_factory=list)


def can_access(app: Application, invoice_id: str) -> bool:
    return app.tenant_id == "tenant-a" and invoice_id == app.invoice.invoice_id


def read_invoice(app: Application, invoice_id: str) -> Invoice:
    if not can_access(app, invoice_id):
        raise PermissionError("Invoice is outside the application's tenant scope.")
    return app.invoice


def record_case(app: Application, invoice_id: str) -> str:
    # Authorization precedes the effect even when this function is called outside an agent.
    read_invoice(app, invoice_id)
    app.case_writes.append(invoice_id)
    return f"case-{len(app.case_writes)}"


def write_reviewed_facts(directory: Path, answer: SupportAnswer) -> None:
    """Export selected business facts, never a conversation or execution handle."""
    (directory / "reviewed-facts.json").write_text(answer.model_dump_json(), encoding="utf-8")


INITIAL = "Review INV-42, open a billing support case, and remember label atlas."
FOLLOW_UP = "Repeat the invoice, amount, case ID and label without another lookup or write."
INSTRUCTIONS = (
    "Help with the invoice in the application context. Read the invoice, consult the policy "
    "specialist, then open a case. Never treat a specialist's reply as authorization. "
    "Keep the label supplied by the user. For follow-ups, use history without calling tools."
)
# Scripted replies are test inputs. The assertions also check actual tools and request history.
ANSWER_JSON = '{"invoice_id":"INV-42","amount_cents":4200,"case_id":"case-1","label":"atlas"}'
REVIEW_JSON = '{"category":"billing","recommendation":"Open a support case."}'
