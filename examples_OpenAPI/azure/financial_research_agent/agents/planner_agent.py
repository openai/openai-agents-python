from pydantic import BaseModel

from src.agents import Agent
from src.agents.model_settings import ModelSettings

PROMPT = (
    "You are a financial research assistant. Given a query about a company, industry, or "
    "financial topic, come up with a set of web searches to perform to best answer the query. "
    "Focus on recent financial news, earnings reports, analyst commentary, and market trends. "
    "Output between 5 and 15 search terms."
)


class FinancialSearchItem(BaseModel):
    reason: str
    "Your reasoning for why this search is important to the financial analysis."

    query: str
    "The search term to use for the web search."


class FinancialSearchPlan(BaseModel):
    searches: list[FinancialSearchItem]
    """A list of web searches to perform to best answer the financial query."""


# Create Azure OpenAI model settings
azure_settings = ModelSettings(
    provider="azure_openai",  # Specify Azure OpenAI as the provider
    temperature=0.7  # Optional: control creativity
)

planner_agent = Agent(
    name="FinancialPlannerAgent",
    instructions=PROMPT,
    output_type=FinancialSearchPlan,
    model_settings=azure_settings,
)
