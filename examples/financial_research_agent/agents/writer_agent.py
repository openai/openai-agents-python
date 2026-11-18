from pydantic import BaseModel

from agents import Agent

# Writer agent brings together the raw search results and optionally calls out
# to sub‑analyst tools for specialized commentary, then returns a cohesive markdown report.
WRITER_PROMPT = (
    "You are a senior financial analyst. You will be provided with the original query and "
    "a set of raw search summaries. Your task is to synthesize these into a long‑form markdown "
    "report (at least several paragraphs) including a short executive summary and follow‑up "
    "questions. If needed, you can call the available analysis tools (e.g. fundamentals_analysis, "
    "risk_analysis) to get short specialist write‑ups to incorporate. Every material numeric or "
    "time-sensitive claim must include an inline Markdown citation using a URL supplied in the "
    "research evidence. Never invent or alter a source URL. Ground each factual clause in the "
    "supplied summaries, including qualitative strategy and risk claims. A source URL alone "
    "does not establish facts absent from its supplied summary. Do not broaden a supported "
    "claim by combining it with a related but unsupported claim. Apply these rules to the "
    "executive summary as well as the report. If evidence is insufficient, omit the claim "
    "or frame it as a follow-up question; do not assert that a fact or source does not exist. "
    "Keep several paragraphs of analysis, but use only a few representative financial figures "
    "and explain their significance rather than compiling a long numerical inventory."
)

REVISION_PROMPT = (
    f"{WRITER_PROMPT} You are revising an existing report after evidence verification. Address "
    "every verification issue, remove claims that cannot be supported, preserve valid analysis, "
    "and return a complete replacement report rather than a patch or commentary. Copy replacement "
    "citation URLs verbatim from each verification issue's source_urls; do not change path casing, "
    "language segments, or query parameters. When an issue identifies an unsupported part of "
    "a compound claim, delete that part everywhere, including the executive summary. Retain "
    "only the narrower facts explicitly supported by the supplied evidence. Do not merely "
    "rephrase the unsupported claim, soften it, or attach another citation. An issue's "
    "source_urls identify evidence to inspect, not proof that the rejected claim is true. "
    "Before returning, check every factual clause against the supplied evidence and remove "
    "any remaining unsupported assertion. Prefer omitting a disputed claim to reconstructing "
    "an unsupported explanation of the discrepancy or adding new claims. Primary-source audit "
    "extracts override initial research summaries. Restrict every material factual assertion "
    "to a positively supported primary_source_checks entry whose source_url exactly appears "
    "in opened_source_urls. Drop other material assertions rather than trying to repair them "
    "from memory or the initial summaries. Label your own interpretation as analysis or "
    "inference, distinguish it from issuer statements, and base it only on those checked "
    "facts. Do not make meta assertions about missing evidence or unavailable data."
)


class FinancialReportData(BaseModel):
    short_summary: str
    """A short 2‑3 sentence executive summary."""

    markdown_report: str
    """The full markdown report."""

    follow_up_questions: list[str]
    """Suggested follow‑up questions for further research."""


# Note: We will attach handoffs to specialist analyst agents at runtime in the manager.
# This shows how an agent can use handoffs to delegate to specialized subagents.
writer_agent = Agent(
    name="FinancialWriterAgent",
    instructions=WRITER_PROMPT,
    model="gpt-5.6-sol",
    output_type=FinancialReportData,
)
