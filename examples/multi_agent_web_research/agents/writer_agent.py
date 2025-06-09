# Agent used to synthesize a final legal report from the individual summaries.
from pydantic import BaseModel

from agents import Agent

PROMPT = (
    "You are a senior legal associate tasked with writing a cohesive legal memorandum or brief for a specific legal query. "
    "You will be provided with the original query, and some initial research (case summaries, statutory excerpts, etc.) done by a research assistant.\n"
    "You should first come up with an outline for the legal document that describes the structure and flow (e.g., Introduction, Facts, Issues, Analysis, Conclusion). "
    "Then, generate the document and return that as your final output.\n"
    "The final output should be in markdown format, and it should be well-structured, legally sound, and detailed. Aim for a comprehensive analysis, citing relevant authorities."
)


class LegalReportData(BaseModel):
    short_summary: str
    """A short 2-3 sentence summary of the legal conclusions."""

    markdown_report: str
    """The final legal memorandum or brief"""

    follow_up_questions: list[str]
    """Suggested legal issues or areas for further research"""


writer_agent = Agent(
    name="LegalWriterAgent",
    instructions=PROMPT,
    model="o3",  # Consider a more powerful model for legal writing if available
    output_type=LegalReportData,
)
