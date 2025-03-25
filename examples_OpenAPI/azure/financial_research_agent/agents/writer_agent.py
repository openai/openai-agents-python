from pydantic import BaseModel

from src.agents import Agent
from src.agents.model_settings import ModelSettings

# Writer agent brings together the raw search results and optionally calls out
# to sub-analyst tools for specialized commentary, then returns a cohesive markdown report.
WRITER_PROMPT = (
    "You are a senior financial analyst. You will be provided with the original query and "
    "a set of raw search summaries. Your task is to synthesize these into a long-form markdown "
    "report (at least several paragraphs) including a short executive summary and follow-up "
    "questions. If needed, you can call the available analysis tools (e.g. fundamentals_analysis, "
    "risk_analysis) to get short specialist write-ups to incorporate."
)


class FinancialReportData(BaseModel):
    short_summary: str
    """A short 2-3 sentence executive summary."""

    markdown_report: str
    """The full markdown report."""

    follow_up_questions: list[str]
    """Suggested follow-up questions for further research."""


# Create Azure OpenAI model settings
azure_settings = ModelSettings(
    provider="azure_openai",  # Specify Azure OpenAI as the provider
    temperature=0.7  # Optional: control creativity
)

# Note: We will attach tools to specialist analyst agents at runtime in the manager.
# This shows how an agent can use tools to delegate to specialized subagents.
writer_agent = Agent(
    name="FinancialWriterAgent",
    instructions=WRITER_PROMPT,
    output_type=FinancialReportData,
    model_settings=azure_settings,
)
