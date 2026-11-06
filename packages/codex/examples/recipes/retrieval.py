"""Answer from a tiny tenant-scoped corpus and check returned source IDs.

    PYTHONPATH=packages/codex python -m examples.recipes.retrieval

The application chooses tenant scope. The model can choose a query, not a
user or tenant. Search returns at most two 240-character passages. A final
source ID must occur in this run's actual search results. That check proves
source membership, not semantic entailment; live answer quality needs its own
evaluation. No embeddings or vector database are required for this tiny corpus.
"""

import asyncio
from dataclasses import dataclass, field

from pydantic import BaseModel

from agents import Agent, NativeProvider, ToolContext, function_tool

CORPUS = (
    ("acme", "acme-hours", "The Acme library opens at 09:00 on weekdays."),
    ("acme", "acme-returns", "The Acme return window is 14 days."),
    ("other", "other-code", "The Other library access code is SYNTHETIC-42."),
)


@dataclass
class SearchScope:
    tenant: str
    retrieved: dict[str, str] = field(default_factory=dict)


class Answer(BaseModel):
    answer: str
    source_ids: list[str]
    found: bool


@function_tool
async def search(ctx: ToolContext[SearchScope], query: str) -> list[dict[str, str]]:
    """Find up to two passages in the application's current tenant."""
    terms = set(query.lower().split())
    matches = []
    for tenant, source_id, passage in CORPUS:
        if tenant == ctx.context.tenant and terms.intersection(passage.lower().split()):
            bounded = passage[:240]
            matches.append({"source_id": source_id, "passage": bounded})
            ctx.context.retrieved[source_id] = bounded
            if len(matches) == 2:
                break
    return matches


def check_sources(answer: Answer, scope: SearchScope) -> Answer:
    if any(source_id not in scope.retrieved for source_id in answer.source_ids):
        raise ValueError("Answer cites a source not retrieved in this run")
    if answer.found != bool(answer.source_ids):
        raise ValueError("Found answers need sources; absent answers must have none")
    return answer


async def answer_question(
    question: str, *, tenant: str, provider: NativeProvider | None = None
) -> tuple[Answer, dict[str, str]]:
    scope = SearchScope(tenant)
    agent = Agent[SearchScope, Answer](
        instructions=(
            "Search the tenant corpus before answering. Use only returned passages. "
            "Return their source_ids. If no passage answers, set found=false, "
            "source_ids=[] and say the answer is unavailable."
        ),
        tools=[search],
        output_type=Answer,
        provider=provider,
    )
    result = await agent.run(question, context=scope)
    return check_sources(result.final_output, scope), dict(scope.retrieved)


async def main() -> None:
    answer, sources = await answer_question("When does the library open?", tenant="acme")
    print(answer.model_dump_json())
    print(sources)


if __name__ == "__main__":
    asyncio.run(main())
