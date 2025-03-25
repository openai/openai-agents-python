from pydantic import BaseModel, Field

from src.agents import Agent
from src.agents.model_settings import ModelSettings

# This agent focuses specifically on financial metrics and KPIs
INSTRUCTIONS = (
    "You are a financial metrics specialist. When given a company name or topic, "
    "extract and analyze key financial metrics from the available information. "
    "Focus on recent performance, trends, and comparison to peers or benchmarks. "
    "Include relevant numerical data like revenue, profit margins, growth rates, P/E ratios, etc. "
    "where available. Be precise, factual, and concise."
)


class AnalysisSummary(BaseModel):
    summary: str = Field(description="A concise financial analysis focusing on key metrics")


# Create Azure OpenAI model settings
azure_settings = ModelSettings(
    provider="azure_openai",  # Specify Azure OpenAI as the provider
    temperature=0.3  # Lower temperature for more factual analysis
)

financials_agent = Agent(
    name="FinancialsAgent",
    instructions=INSTRUCTIONS,
    output_type=AnalysisSummary,
    model_settings=azure_settings,
)
