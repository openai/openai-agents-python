"""Preserve explicit one-shot choices without replacing custom opening defaults."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from agents import Agent


class OpeningObserved(Exception):
    """Stop at the public dispatch boundary before acquiring runtime resources."""


class ConfiguredAgent(Agent):
    @asynccontextmanager
    async def session(
        self,
        *,
        observation_retention="all",
        max_function_concurrency=1,
        **kwargs: Any,
    ):
        raise OpeningObserved(observation_retention, max_function_concurrency)
        yield


@pytest.mark.parametrize("call", ["run", "run_sync", "overridden_run_sync"])
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, ("all", 1)),
        ({"observation_retention": "bounded"}, ("bounded", 1)),
        ({"max_function_concurrency": None}, ("all", None)),
        (
            {"observation_retention": "bounded", "max_function_concurrency": None},
            ("bounded", None),
        ),
        ({"observation_retention": "all", "max_function_concurrency": 2}, ("all", 2)),
    ],
    ids=["omitted", "bounded", "unlimited", "explicit-defaults", "nondefaults"],
)
def test_one_shot_options_preserve_omission_and_explicit_values(call, options, expected):
    class ConfiguredRunAgent(Agent):
        async def run(
            self,
            input,
            *,
            observation_retention="all",
            max_function_concurrency=1,
            **kwargs: Any,
        ):
            raise OpeningObserved(observation_retention, max_function_concurrency)

    agent = (
        ConfiguredRunAgent(instructions="Answer.")
        if call == "overridden_run_sync"
        else ConfiguredAgent(instructions="Answer.")
    )
    with pytest.raises(OpeningObserved) as observed:
        if call == "run":
            asyncio.run(agent.run("Synthetic input", **options))
        else:
            agent.run_sync("Synthetic input", **options)
    assert observed.value.args == expected
