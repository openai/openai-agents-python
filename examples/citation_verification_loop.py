"""
Citation-verification loop pattern.

This example demonstrates a multi-agent pipeline that guards against
unverifiable claims before including them in a final summary.

Pipeline:
1. ResearchAgent  — given a topic, generates 5 factual claims as a structured list.
2. VerifierAgent  — for each claim, asks the model whether it can cite a specific
   source (publication, URL, or author).  Returns a structured verdict with a
   ``verifiable`` flag and an optional source string.
3. Loop           — claims marked ``verifiable=False`` are dropped.
4. SynthesisAgent — receives only the verified claims and writes a final summary
   with inline citations.

When to use this pattern:
- Research or fact-checking pipelines where source attribution is required.
- Content generation workflows that must not propagate hallucinations.
- Any setting where downstream consumers need confidence that each assertion
  has a traceable origin.

Run with:
    OPENAI_API_KEY=sk-... uv run python -m examples.citation_verification_loop
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, model_validator

from agents import Agent, ItemHelpers, Runner, trace
from examples.auto_mode import input_with_fallback

# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------


class ClaimList(BaseModel):
    claims: list[str]


class VerificationResult(BaseModel):
    claim: str
    verifiable: bool
    source_if_any: str  # empty string when verifiable is False

    @model_validator(mode="after")
    def require_source_when_verifiable(self) -> "VerificationResult":
        if self.verifiable and not self.source_if_any.strip():
            raise ValueError(
                "source_if_any must be a non-empty citation when verifiable is True"
            )
        return self


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

research_agent = Agent(
    name="research_agent",
    instructions=(
        "You are a research assistant. Given a topic, generate exactly 5 concise "
        "factual claims about it. Return them as a JSON object with a single key "
        '"claims" whose value is a list of 5 strings. Each claim should be a '
        "specific, testable statement — not a vague generality."
    ),
    output_type=ClaimList,
)

verifier_agent = Agent(
    name="verifier_agent",
    instructions=(
        "You are a fact-checking assistant. Given a single factual claim, decide "
        "whether you can cite a specific, real source for it — such as a named "
        "publication, a URL, or an author and year. "
        "If you can provide a concrete source, set verifiable=true and populate "
        "source_if_any with that citation. "
        "If you cannot point to a specific, verifiable source, set verifiable=false "
        "and source_if_any to an empty string. Mark a claim as UNVERIFIABLE rather "
        "than fabricating a source."
    ),
    output_type=VerificationResult,
)

synthesis_agent = Agent(
    name="synthesis_agent",
    instructions=(
        "You are a technical writer. Given a list of verified claims, each paired "
        "with its source, write a concise, well-structured paragraph that summarises "
        "the findings and includes an inline citation after each fact in the format "
        "(Source: <citation>). Do not introduce any claims that are not in the list."
    ),
)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


async def main() -> None:
    topic = input_with_fallback(
        "Enter a research topic: ",
        "the health effects of intermittent fasting",
    )

    with trace("Citation verification loop"):
        # Step 1: generate claims
        research_result = await Runner.run(research_agent, topic)
        assert isinstance(research_result.final_output, ClaimList)
        claims = research_result.final_output.claims
        print(f"\n[ResearchAgent] Generated {len(claims)} claims:")
        for i, c in enumerate(claims, 1):
            print(f"  {i}. {c}")

        # Step 2: verify each claim individually
        # Store (original_claim, source) tuples so the synthesis prompt always
        # uses the research agent's exact wording, not a rephrased verifier copy.
        verified: list[tuple[str, str]] = []
        dropped: list[str] = []

        print("\n[VerifierAgent] Checking each claim …")
        for claim in claims:
            result = await Runner.run(verifier_agent, claim)
            assert isinstance(result.final_output, VerificationResult)
            verdict = result.final_output
            if verdict.verifiable:
                print(f"  ✓ VERIFIABLE — {claim[:60]}…")
                print(f"      Source: {verdict.source_if_any}")
                verified.append((claim, verdict.source_if_any))
            else:
                print(f"  ✗ UNVERIFIABLE — {claim[:60]}…")
                dropped.append(claim)

        print(f"\nVerified: {len(verified)}  |  Dropped: {len(dropped)}")

        if not verified:
            print("\nNo verifiable claims remain. Stopping.")
            return

        # Step 3: synthesise a final summary from verified claims only
        verified_block = "\n".join(f"- {c} (Source: {s})" for c, s in verified)
        synthesis_prompt = f"Topic: {topic}\n\nVerified claims with sources:\n{verified_block}"

        synthesis_result = await Runner.run(synthesis_agent, synthesis_prompt)
        summary = ItemHelpers.text_message_outputs(synthesis_result.new_items)

        print("\n[SynthesisAgent] Final summary:")
        print(summary)

        if dropped:
            print("\n[Dropped claims — no verifiable source found]:")
            for d in dropped:
                print(f"  - {d}")


if __name__ == "__main__":
    asyncio.run(main())
