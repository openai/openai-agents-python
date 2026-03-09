"""Structured output examples using Pydantic models.

Demonstrates how to get typed, validated responses from agents using
Pydantic BaseModel, dataclasses, custom output schemas, and non-strict schemas.

Setup:
    export OPENAI_API_KEY="your-api-key"

Usage:
    python examples/sdk_examples/structured_output.py
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from agents import Agent, AgentOutputSchema, AgentOutputSchemaBase, Runner


# --- Example 1: Pydantic BaseModel Output ---


class MovieReview(BaseModel):
    """Structured movie review output."""

    title: str = Field(description="The movie title")
    rating: float = Field(description="Rating from 1.0 to 10.0")
    pros: list[str] = Field(description="List of positive aspects")
    cons: list[str] = Field(description="List of negative aspects")
    recommendation: str = Field(description="Brief recommendation")


async def example_pydantic_output() -> None:
    """Get structured output as a Pydantic model."""
    agent = Agent(
        name="Movie Critic",
        instructions="You are a movie critic. Review movies with structured analysis.",
        output_type=MovieReview,
    )

    result = await Runner.run(agent, "Review the movie 'Inception' by Christopher Nolan.")
    review: MovieReview = result.final_output

    print(f"[Pydantic] Title: {review.title}")
    print(f"[Pydantic] Rating: {review.rating}/10")
    print(f"[Pydantic] Pros: {', '.join(review.pros)}")
    print(f"[Pydantic] Cons: {', '.join(review.cons)}")
    print(f"[Pydantic] Recommendation: {review.recommendation}")


# --- Example 2: Nested Pydantic Models ---


class Ingredient(BaseModel):
    name: str = Field(description="Ingredient name")
    amount: str = Field(description="Amount with unit")


class Recipe(BaseModel):
    """A structured recipe."""

    name: str = Field(description="Recipe name")
    servings: int = Field(description="Number of servings")
    prep_time_minutes: int = Field(description="Preparation time in minutes")
    ingredients: list[Ingredient] = Field(description="List of ingredients")
    steps: list[str] = Field(description="Ordered cooking steps")


async def example_nested_models() -> None:
    """Get structured output with nested Pydantic models."""
    agent = Agent(
        name="Chef",
        instructions="You are a chef. Provide recipes in structured format.",
        output_type=Recipe,
    )

    result = await Runner.run(agent, "Give me a simple recipe for scrambled eggs.")
    recipe: Recipe = result.final_output

    print(f"[Nested] {recipe.name} ({recipe.servings} servings, {recipe.prep_time_minutes} min)")
    print("[Nested] Ingredients:")
    for ing in recipe.ingredients:
        print(f"  - {ing.amount} {ing.name}")
    print("[Nested] Steps:")
    for i, step in enumerate(recipe.steps, 1):
        print(f"  {i}. {step}")


# --- Example 3: Enum-like Structured Output ---


class SentimentAnalysis(BaseModel):
    """Sentiment analysis result."""

    text: str = Field(description="The analyzed text")
    sentiment: str = Field(description="One of: positive, negative, neutral")
    confidence: float = Field(description="Confidence score from 0.0 to 1.0")
    key_phrases: list[str] = Field(description="Key phrases that influenced the analysis")


async def example_classification() -> None:
    """Use structured output for classification tasks."""
    agent = Agent(
        name="Sentiment Analyzer",
        instructions=(
            "Analyze the sentiment of the given text. "
            "Classify as 'positive', 'negative', or 'neutral'."
        ),
        output_type=SentimentAnalysis,
    )

    texts = [
        "I absolutely love this product! Best purchase ever!",
        "The service was terrible and the food was cold.",
        "The meeting is scheduled for 3 PM tomorrow.",
    ]

    for text in texts:
        result = await Runner.run(agent, text)
        analysis: SentimentAnalysis = result.final_output
        print(f"[Classification] '{text[:40]}...' -> {analysis.sentiment} ({analysis.confidence})")


# --- Example 4: Dataclass Output ---


@dataclass
class TaskBreakdown:
    """Break a task into subtasks."""

    original_task: str
    subtasks: list[str]
    estimated_hours: int


async def example_dataclass_output() -> None:
    """Use a dataclass as the output type."""
    agent = Agent(
        name="Project Planner",
        instructions="Break down tasks into subtasks with time estimates.",
        output_type=TaskBreakdown,
    )

    result = await Runner.run(agent, "Build a personal blog website")
    breakdown: TaskBreakdown = result.final_output

    print(f"[Dataclass] Task: {breakdown.original_task}")
    print(f"[Dataclass] Estimated hours: {breakdown.estimated_hours}")
    for i, subtask in enumerate(breakdown.subtasks, 1):
        print(f"  {i}. {subtask}")


# --- Example 5: Non-Strict Output Schema ---


@dataclass
class FlexibleOutput:
    """An output type with dict fields that aren't strict-compatible."""

    data: dict[int, str]


async def example_non_strict_output() -> None:
    """Use a non-strict output schema for types that can't use strict mode."""
    agent = Agent(
        name="Data Generator",
        instructions="Generate numbered items as requested.",
        # Use AgentOutputSchema with strict_json_schema=False for non-strict-compatible types.
        output_type=AgentOutputSchema(FlexibleOutput, strict_json_schema=False),
    )

    result = await Runner.run(agent, "List 3 colors numbered 1, 2, 3.")
    print(f"[Non-Strict] Output: {result.final_output}")


# --- Example 6: Custom Output Schema ---


class MarkdownOutputSchema(AgentOutputSchemaBase):
    """Custom schema that extracts a specific field from JSON output."""

    def is_plain_text(self) -> bool:
        return False

    def name(self) -> str:
        return "MarkdownOutput"

    def json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        }

    def is_strict_json_schema(self) -> bool:
        return True

    def validate_json(self, json_str: str) -> Any:
        """Parse JSON and format as markdown."""
        data = json.loads(json_str)
        return f"# {data['title']}\n\n{data['body']}"


async def example_custom_schema() -> None:
    """Use a completely custom output schema."""
    agent = Agent(
        name="Writer",
        instructions="Write a short article with a title and body.",
        output_type=MarkdownOutputSchema(),
    )

    result = await Runner.run(agent, "Write about the benefits of walking.")
    print(f"[Custom Schema] Output:\n{result.final_output}")


# --- Run all examples ---


async def main() -> None:
    print("=" * 60)
    print("AGENTS SDK - Structured Output Examples")
    print("=" * 60)

    examples = [
        ("1. Pydantic BaseModel", example_pydantic_output),
        ("2. Nested Models", example_nested_models),
        ("3. Classification", example_classification),
        ("4. Dataclass Output", example_dataclass_output),
        ("5. Non-Strict Schema", example_non_strict_output),
        ("6. Custom Schema", example_custom_schema),
    ]

    for title, example_fn in examples:
        print(f"\n--- {title} ---")
        await example_fn()


if __name__ == "__main__":
    asyncio.run(main())
