from pydantic import BaseModel

from agents import Agent, ModelSettings, WebSearchTool

# Given a search term, use web search to pull back a brief summary.
# Summaries should be concise but capture the main financial points.
INSTRUCTIONS = (
    "You are a research assistant specializing in financial topics. "
    "Given a search term, use web search to retrieve up‑to‑date context and "
    "produce a short summary of at most 300 words. Focus on key numbers, events, "
    "or quotes that will be useful to a financial analyst."
)


class FinancialSearchSummary(BaseModel):
    summary: str
    """A concise summary of the search findings."""


search_agent = Agent(
    name="FinancialSearchAgent",
    model="gpt-5.6-sol",
    instructions=INSTRUCTIONS,
    tools=[WebSearchTool()],
    model_settings=ModelSettings(response_include=["web_search_call.action.sources"]),
    output_type=FinancialSearchSummary,
)
