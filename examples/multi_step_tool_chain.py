"""
Multi-step tool chain pattern.

This example demonstrates a sequential tool dependency chain where the output
of one tool becomes the required input to the next.  The chain models a
real-world "search → fetch → extract → rank" research workflow in which each
step must succeed before the following step can run.

Pipeline executed by ResearchAgent:
1. search_documents  — discover relevant documents for a query.
2. fetch_document    — retrieve the full content of the best match.
3. extract_key_facts — parse the document content into structured facts.
4. rank_facts_by_relevance — order the facts by relevance to the original query.

When to use this pattern:
- Pipelines with hard data-flow dependencies (output of A is the only valid
  input for B).
- Workflows that need an audit trail showing exactly which tool ran and with
  what arguments.
- Any sequential processing chain where skipping a step would produce
  meaningless or invalid results.

Run with:
    OPENAI_API_KEY=sk-... uv run python examples/multi_step_tool_chain.py
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agents import Agent, ModelSettings, Runner, function_tool, trace

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

# Mock document store used by search_documents and fetch_document.
_MOCK_DOCUMENTS: dict[str, dict[str, str]] = {
    "d1": {
        "title": "Introduction to Transformer Architectures",
        "snippet": "Transformers use self-attention to model long-range dependencies.",
        "content": (
            "Transformer architectures, introduced by Vaswani et al. in 2017, "
            "replaced recurrent networks with a fully attention-based design. "
            "The core building block is multi-head self-attention, which allows "
            "every token in a sequence to attend to every other token in parallel. "
            "This parallelism dramatically speeds up training on modern GPU hardware. "
            "Positional encodings are added to token embeddings so the model can "
            "capture word order. The encoder stack maps an input sequence to a "
            "continuous representation; the decoder stack attends to both the encoder "
            "output and previously generated tokens to produce the next token. "
            "Pre-trained transformer models such as BERT and GPT-2 demonstrated "
            "that large-scale unsupervised pre-training followed by task-specific "
            "fine-tuning yields state-of-the-art results across many NLP benchmarks. "
            "Subsequent scaling work showed that performance continues to improve "
            "with model size and training data, leading to the current generation "
            "of large language models."
        ),
    },
    "d2": {
        "title": "Retrieval-Augmented Generation (RAG) Explained",
        "snippet": "RAG combines dense retrieval with generative language models.",
        "content": (
            "Retrieval-Augmented Generation (RAG) enhances language model outputs "
            "by grounding responses in documents retrieved at inference time. "
            "A dense retriever encodes both the query and a corpus of documents "
            "into a shared embedding space; the top-k most similar documents are "
            "then passed as additional context to a generative model. "
            "This approach reduces hallucination because the model can cite "
            "retrieved passages rather than relying solely on parametric memory. "
            "RAG is especially valuable for knowledge-intensive tasks such as "
            "open-domain question answering and fact verification, where the "
            "information needed may not have been present in the training data. "
            "Modern RAG pipelines often include a reranker to refine the initial "
            "retrieval results and chunk-level metadata to improve source attribution. "
            "Hybrid retrieval — combining sparse BM25 with dense vectors — further "
            "improves recall on queries that contain rare or domain-specific terms."
        ),
    },
    "d3": {
        "title": "Prompt Engineering Best Practices",
        "snippet": "Structured prompts with examples improve LLM reliability.",
        "content": (
            "Prompt engineering is the practice of crafting inputs to language "
            "models to elicit accurate, consistent, and useful outputs. "
            "Few-shot prompting includes worked examples in the prompt so the "
            "model can infer the desired output format from demonstration. "
            "Chain-of-thought prompting asks the model to reason step-by-step "
            "before producing a final answer, which markedly improves performance "
            "on arithmetic and logical reasoning tasks. "
            "System prompts establish role, tone, and constraints and are "
            "processed before any user turn. "
            "Temperature and top-p sampling parameters control output diversity: "
            "low temperature produces more deterministic completions while higher "
            "values increase creativity. "
            "Structured output schemas (JSON mode or function calling) reduce "
            "post-processing effort and make downstream parsing more robust. "
            "Iterative prompt refinement — testing, identifying failure modes, "
            "and editing — is more reliable than trying to write a perfect prompt "
            "on the first attempt."
        ),
    },
}


@function_tool
def search_documents(query: str) -> str:
    """Search a document store and return a list of matching document summaries.

    Args:
        query: The search query string.

    Returns:
        A JSON string containing a list of document objects, each with an
        ``id``, ``title``, and ``snippet`` field.
    """
    print(f"[Tool: search_documents] query={query!r}")
    results = [
        {"id": doc_id, "title": meta["title"], "snippet": meta["snippet"]}
        for doc_id, meta in _MOCK_DOCUMENTS.items()
    ]
    output = json.dumps(results, indent=2)
    print(f"[Tool: search_documents] returned {len(results)} document(s)")
    return output


@function_tool
def fetch_document(document_id: str) -> str:
    """Fetch the full text content of a document by its ID.

    Args:
        document_id: The ID of the document to retrieve (e.g. ``"d1"``).

    Returns:
        The full content of the document as a plain string, or an error
        message if the ID is not found.
    """
    print(f"[Tool: fetch_document] document_id={document_id!r}")
    if document_id not in _MOCK_DOCUMENTS:
        error = f"Error: document '{document_id}' not found. Valid IDs: {list(_MOCK_DOCUMENTS)}"
        print(f"[Tool: fetch_document] {error}")
        return error
    content = _MOCK_DOCUMENTS[document_id]["content"]
    print(f"[Tool: fetch_document] fetched {len(content)} chars for {document_id!r}")
    return content


@function_tool
def extract_key_facts(document_content: str) -> str:
    """Extract key facts from document content and return them as structured JSON.

    In a production system the model would send the document through a
    structured-output LLM call.  Here we return mock facts derived from a
    simple keyword scan so the example runs without additional API calls.

    Args:
        document_content: The raw text of the document to analyse.

    Returns:
        A JSON string containing a list of fact objects, each with a ``fact``
        string and a ``confidence`` float between 0.0 and 1.0.
    """
    print(f"[Tool: extract_key_facts] processing {len(document_content)} chars")

    # Derive mock facts from the document's own sentences.
    sentences = [
        s.strip() for s in document_content.replace("\n", " ").split(".") if len(s.strip()) > 30
    ]
    mock_facts = (
        [
            {"fact": sentences[0] + ".", "confidence": 0.95},
            {"fact": sentences[1] + ".", "confidence": 0.91},
            {"fact": sentences[2] + ".", "confidence": 0.88},
            {"fact": sentences[3] + ".", "confidence": 0.82},
            {"fact": sentences[4] + ".", "confidence": 0.76},
        ]
        if len(sentences) >= 5
        else [
            {"fact": s + ".", "confidence": round(0.9 - i * 0.05, 2)}
            for i, s in enumerate(sentences[:5])
        ]
    )

    output = json.dumps(mock_facts, indent=2)
    print(f"[Tool: extract_key_facts] extracted {len(mock_facts)} fact(s)")
    return output


@function_tool
def rank_facts_by_relevance(facts_json: str, original_query: str) -> str:
    """Rank extracted facts by relevance to the original search query.

    Relevance is estimated by counting query-term overlaps in each fact.
    A ``rank`` field (1 = most relevant) and a ``relevance_score`` float are
    added to each item and the list is returned sorted best-first.

    Args:
        facts_json: A JSON string produced by ``extract_key_facts``.
        original_query: The original search query used to seed the pipeline.

    Returns:
        A JSON string of fact objects sorted by relevance, each augmented with
        ``rank`` and ``relevance_score`` fields.
    """
    print(f"[Tool: rank_facts_by_relevance] query={original_query!r}")
    facts: list[dict[str, Any]] = json.loads(facts_json)
    query_terms = set(original_query.lower().split())

    for fact in facts:
        fact_words = set(fact["fact"].lower().split())
        overlap = len(query_terms & fact_words)
        # Blend term-overlap signal with the pre-existing confidence score.
        fact["relevance_score"] = round(
            fact["confidence"] * 0.6 + min(overlap / max(len(query_terms), 1), 1.0) * 0.4, 3
        )

    ranked = sorted(facts, key=lambda f: f["relevance_score"], reverse=True)
    for position, fact in enumerate(ranked, start=1):
        fact["rank"] = position

    output = json.dumps(ranked, indent=2)
    print(f"[Tool: rank_facts_by_relevance] ranked {len(ranked)} fact(s)")
    return output


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

research_agent = Agent(
    name="ResearchAgent",
    model="gpt-4o-mini",
    model_settings=ModelSettings(parallel_tool_calls=False),
    tools=[search_documents, fetch_document, extract_key_facts, rank_facts_by_relevance],
    instructions=(
        "You are a research assistant. When given a research query:\n"
        "1) Call search_documents with the query to discover relevant documents.\n"
        "2) Call fetch_document with the ID of the most relevant result.\n"
        "3) Call extract_key_facts with the fetched document content.\n"
        "4) Call rank_facts_by_relevance with the extracted facts JSON and the "
        "original query.\n"
        "Use the tools in this exact sequence — never skip a step. "
        "After all four tools have run, present the top-ranked facts in a "
        "concise, readable summary."
    ),
)


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------


async def run_research(query: str) -> None:
    """Run the research agent for a single query and print the result.

    Args:
        query: The research question to investigate.
    """
    print(f"\n{'=' * 60}")
    print(f"Query: {query}")
    print("=" * 60)

    result = await Runner.run(research_agent, query)

    print(f"\n[ResearchAgent] Final answer:\n{result.final_output}")


async def main() -> None:
    """Run the multi-step tool chain for two different research queries."""
    queries = [
        "How do transformer architectures work?",
        "What are the best practices for prompt engineering?",
    ]

    with trace("multi_step_tool_chain"):
        for query in queries:
            await run_research(query)


if __name__ == "__main__":
    asyncio.run(main())
