"""
Structured output validation loop pattern.

This example demonstrates the "generate-validate-retry" pattern: an agent
produces structured output, business-rule checks are applied, and if violations
are found the agent is re-run with a correction prompt that lists the exact
problems.  This is distinct from schema-level validation (which the SDK handles
automatically) — it handles domain-level invariants that JSON Schema cannot
express.

Pattern steps:
1. Define a Pydantic output model (``ProductReview``) with typed fields.
2. Write a ``validate_review`` function that returns a list of human-readable
   violation strings for any business-rule failures.
3. Configure a module-level ``Agent`` with ``output_type=ProductReview``.
4. ``analyze_with_validation`` runs the agent, checks violations, and if any
   are found it builds a correction prompt that quotes the specific errors and
   re-runs the agent — up to ``max_attempts`` times.
5. ``main`` exercises the loop against three representative review texts and
   wraps the whole run in a trace for observability.

When to use this pattern:
- Structured extraction pipelines where schema alone cannot enforce semantic
  constraints (e.g. sentiment/score consistency, minimum content length).
- Any workflow that needs deterministic post-generation quality gates before
  the output is consumed downstream.
- Agent chains where a malformed first-step output would silently corrupt
  later steps.

Run with:
    OPENAI_API_KEY=sk-... uv run python examples/structured_output_validation_loop.py
"""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel

from agents import Agent, Runner, trace

# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


class ProductReview(BaseModel):
    """Structured sentiment analysis extracted from a product review."""

    sentiment: Literal["positive", "neutral", "negative"]
    """Overall sentiment of the review."""

    score: int
    """Numeric quality score in the range 1–10."""

    key_points: list[str]
    """Key points mentioned in the review (at least one item required)."""

    recommendation: str
    """A recommendation sentence directed at potential buyers."""


# ---------------------------------------------------------------------------
# Business-rule validator
# ---------------------------------------------------------------------------


def validate_review(review: ProductReview) -> list[str]:
    """Return a list of business-rule violations for *review*.

    An empty list means the review passes all checks.
    """
    violations: list[str] = []

    # Score must be in range 1-10.  Checked here (not as a field_validator) so an
    # out-of-range score triggers a correction prompt + retry rather than aborting.
    if not 1 <= review.score <= 10:
        violations.append(
            f"score {review.score} is out of range; it must be between 1 and 10 inclusive."
        )
        # Skip sentiment/score consistency checks when the score itself is invalid.
        return violations

    # Sentiment/score consistency checks.
    # Agent instructions define: positive = 7-10, neutral = 5-6, negative = 1-4.
    # Validate the full bands so every mismatch triggers a retry.
    if review.sentiment == "positive" and review.score < 7:
        violations.append(
            f"Inconsistent output: sentiment is 'positive' but score is {review.score} "
            f"(positive requires a score of 7 or higher; 5-6 is neutral, 1-4 is negative)."
        )
    elif review.sentiment == "negative" and review.score > 4:
        violations.append(
            f"Inconsistent output: sentiment is 'negative' but score is {review.score} "
            f"(negative requires a score of 4 or lower; 5-6 is neutral, 7-10 is positive)."
        )
    elif review.sentiment == "neutral" and not (5 <= review.score <= 6):
        violations.append(
            f"Inconsistent output: sentiment is 'neutral' but score is {review.score} "
            f"(neutral requires a score of 5 or 6)."
        )

    # key_points must contain at least one item.
    if not review.key_points:
        violations.append("key_points must contain at least one item but the list is empty.")

    # Recommendation must be at least 10 words to be useful.
    word_count = len(review.recommendation.split())
    if word_count < 10:
        violations.append(
            f"recommendation is too short ({word_count} words); "
            f"it must be at least 10 words to be actionable."
        )

    return violations


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

review_agent = Agent(
    name="ReviewAnalyzer",
    model="gpt-4o-mini",
    output_type=ProductReview,
    instructions=(
        "Analyze the product review text provided by the user and extract structured "
        "sentiment data. Be consistent: a 'positive' sentiment must correspond to a "
        "score of 7 or higher; a 'negative' sentiment must correspond to a score of "
        "4 or lower; 'neutral' maps to 5–6. Always include at least one key point, "
        "and write a recommendation of at least 10 words aimed at potential buyers."
    ),
)

# ---------------------------------------------------------------------------
# Validation loop
# ---------------------------------------------------------------------------


async def analyze_with_validation(
    text: str,
    max_attempts: int = 3,
) -> ProductReview:
    """Run ``review_agent`` on *text*, retrying if business rules are violated.

    On each retry the correction prompt includes the original review text plus
    a numbered list of the exact violations found so the model can fix them.

    Args:
        text: Raw product review text to analyse.
        max_attempts: Maximum number of agent invocations before giving up.

    Returns:
        The first ``ProductReview`` that passes all business-rule checks, or
        the last output produced if all attempts are exhausted.

    Raises:
        RuntimeError: If the agent fails to produce a valid output in all
            attempts and ``max_attempts`` is exhausted.
    """
    current_input = text
    last_output: ProductReview | None = None

    for attempt in range(1, max_attempts + 1):
        print(f"  [attempt {attempt}/{max_attempts}] Running ReviewAnalyzer …")

        result = await Runner.run(review_agent, current_input)
        assert isinstance(result.final_output, ProductReview)
        review = result.final_output
        last_output = review

        violations = validate_review(review)

        if not violations:
            print(f"  [attempt {attempt}] Passed all business-rule checks.")
            return review

        # Build a correction prompt that quotes the violations.
        violation_lines = "\n".join(f"  {i}. {v}" for i, v in enumerate(violations, start=1))
        print(f"  [attempt {attempt}] Found {len(violations)} violation(s):")
        for v in violations:
            print(f"    - {v}")

        if attempt < max_attempts:
            current_input = (
                f"Original review text:\n{text}\n\n"
                f"Your previous analysis had the following business-rule violations "
                f"that you must fix:\n{violation_lines}\n\n"
                f"Please re-analyze the review and return a corrected ProductReview "
                f"that satisfies all constraints."
            )

    # All attempts exhausted — return the last output so the caller can decide.
    assert last_output is not None
    print(
        f"  [warning] Returning last output after {max_attempts} attempt(s); "
        f"some violations may remain."
    )
    return last_output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    sample_reviews = [
        (
            "Positive review",
            (
                "This blender is absolutely fantastic! The motor is incredibly powerful "
                "and handles ice cubes with ease. Cleanup is a breeze thanks to the "
                "self-cleaning mode. I use it every morning for smoothies and it has "
                "never let me down. Five stars — worth every penny!"
            ),
        ),
        (
            "Negative review with inconsistent signals",
            (
                "Terrible product. It broke after two weeks of light use. The blades "
                "became loose and started rattling, and customer support was unhelpful. "
                "I had to return it. Would not recommend to anyone."
            ),
        ),
        (
            "Ambiguous review",
            (
                "It is okay, I guess. The design looks nice and it works most of the "
                "time, but sometimes it leaks from the bottom and the noise is louder "
                "than I expected. Might be fine for occasional use but I am not sure "
                "if I would buy it again."
            ),
        ),
    ]

    with trace("structured_output_validation"):
        for label, review_text in sample_reviews:
            print(f"\n{'=' * 60}")
            print(f"Review: {label}")
            print(f"{'=' * 60}")
            print(f"Text: {review_text[:100]}{'…' if len(review_text) > 100 else ''}\n")

            final: ProductReview = await analyze_with_validation(review_text)

            print("\n  Result:")
            print(f"    sentiment    : {final.sentiment}")
            print(f"    score        : {final.score}/10")
            print(f"    key_points   : {final.key_points}")
            print(f"    recommendation: {final.recommendation}")


if __name__ == "__main__":
    asyncio.run(main())
