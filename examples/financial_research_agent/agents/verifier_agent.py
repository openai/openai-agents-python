from typing import Literal

from pydantic import BaseModel

from agents import Agent

# Agent to sanity‑check a synthesized report for consistency and recall.
# This can be used to flag potential gaps or obvious mistakes.
VERIFIER_PROMPT = (
    "You are a meticulous evidence auditor. You will receive an original request, an explicit "
    "research cutoff date, a financial report, and structured web research evidence with source "
    "URLs and an independent primary_source_audit with opened_source_urls. Treat that audit’s "
    "document extracts as authoritative over initial summaries when they disagree. Reject "
    "claims that its primary documents contradict or cannot support. Judge the report only against that supplied evidence; do not reject or approve claims "
    "based on your own memory. Check that material numeric and time-sensitive claims are supported "
    "by the evidence, that citations use supplied URLs, that the report is internally consistent, "
    "and that uncertainty is appropriately caveated. The payload includes allowed_source_urls; "
    "compare citation URL strings exactly against that list. If a citation exactly matches an "
    "allowed URL, accept the URL even when it contains a tracking parameter or another allowed "
    "variant exists. Treat information published on or before the research cutoff as potentially "
    "available. Mark unsupported claims separately from claims that the evidence directly "
    "contradicts. Evaluate explicitly labeled analyst inference as reasoning from supported "
    "premises, not as a claim that the issuer said the same thing. Continue rejecting "
    "unsupported factual premises, forecasts, and causal assertions. Editorial headings "
    "and report-scope labels are not issuer financial claims."
)


class VerificationIssue(BaseModel):
    claim: str
    """The report claim that needs attention."""

    category: Literal["unsupported", "contradicted", "stale_or_unreleased", "other"]
    """The evidence problem associated with the claim."""

    explanation: str
    """Why the evidence does not support the claim."""

    source_urls: list[str]
    """Relevant supplied source URLs, if any."""


class VerificationResult(BaseModel):
    verified: bool
    """Whether the report is coherent and supported by the supplied evidence."""

    issues: list[VerificationIssue]
    """Evidence-based issues that must be corrected before publication."""


verifier_agent = Agent(
    name="VerificationAgent",
    instructions=VERIFIER_PROMPT,
    model="gpt-5.6-sol",
    output_type=VerificationResult,
)
