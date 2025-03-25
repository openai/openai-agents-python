from pydantic import BaseModel, Field

from src.agents import Agent
from src.agents.model_settings import ModelSettings

# This agent specializes in identifying potential risks and red flags
INSTRUCTIONS = (
    "You are a financial risk analyst. When given a company name or topic, "
    "identify potential risks, challenges, or red flags from the available information. "
    "Consider regulatory issues, market headwinds, competitive threats, financial stability "
    "concerns, and other factors that could negatively impact performance or valuation. "
    "Be balanced and evidence-based in your assessment."
)


class AnalysisSummary(BaseModel):
    summary: str = Field(
        description="A concise risk analysis highlighting potential concerns"
    )


# Create Azure OpenAI model settings
azure_settings = ModelSettings(
    provider="azure_openai",  # Specify Azure OpenAI as the provider
    temperature=0.4  # Moderate temperature for balanced risk assessment
)

risk_agent = Agent(
    name="RiskAnalysisAgent",
    instructions=INSTRUCTIONS,
    output_type=AnalysisSummary,
    model_settings=azure_settings,
)
