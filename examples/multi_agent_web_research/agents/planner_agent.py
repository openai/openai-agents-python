from pydantic import BaseModel

from agents import Agent

PROMPT = (
    "You are a helpful legal research assistant. Given a legal query, come up with a set of web searches "
    "to perform to find relevant case law, statutes, and legal precedents. Output between 5 and 10 terms to query for."
)


class LegalSearchItem(BaseModel):
    reason: str
    "Your reasoning for why this search is important to the legal query."

    query: str
    "The search term to use for the web search (e.g., specific case names, legal doctrines, statutory provisions)."


class LegalSearchPlan(BaseModel):
    searches: list[LegalSearchItem]
    """A list of web searches to perform to best answer the legal query."""


planner_agent = Agent(
    name="LegalPlannerAgent",
    instructions=PROMPT,
    model="o4-mini",
    output_type=LegalSearchPlan,
)
