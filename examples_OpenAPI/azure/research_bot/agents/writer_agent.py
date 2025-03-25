from pydantic import BaseModel

from src.agents import Agent
from src.agents.model_settings import ModelSettings

INSTRUCTIONS = (
    "You are a professional report writer with expertise in synthesizing information. "
    "Given an original query and a set of search results, create a comprehensive markdown report "
    "that answers the query. The report should be well-structured with sections, have a logical "
    "flow, and be informative. Include a brief executive summary at the beginning. "
    "Also include 3-5 follow-up questions that the user might want to ask next. "
    "The report should be written in professional language, avoiding unnecessary jargon, "
    "and be engaging to read."
)


class ReportData(BaseModel):
    short_summary: str
    """A short 2-3 sentence executive summary."""

    markdown_report: str
    """The full markdown report."""

    follow_up_questions: list[str]
    """Suggested follow-up questions for further research."""


# Create Azure OpenAI model settings
azure_settings = ModelSettings(
    provider="azure_openai",  # Specify Azure OpenAI as the provider
    temperature=0.7  # Control creativity
)

writer_agent = Agent(
    name="WriterAgent",
    instructions=INSTRUCTIONS,
    output_type=ReportData,
    model_settings=azure_settings,
)
