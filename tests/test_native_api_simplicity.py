"""Removed knobs and discoverability through real public entry points."""

import asyncio
import subprocess
import sys
from typing import Any

import pytest
from pydantic import ValidationError

from agents import Agent
from agents.state import SessionState


def test_manual_interaction_wildcard_import_matches_package_root():
    from agents import Interaction

    namespace: dict[str, object] = {}
    exec("from agents.manual import *", namespace)
    assert namespace["Interaction"] is Interaction


def test_lazy_discovery_in_a_fresh_interpreter():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.util
import sys
import agents

before = set(sys.modules)
assert set(agents.__all__) <= set(dir(agents))
assert set(sys.modules) == before
assert 'agents._native' not in sys.modules
assert importlib.util.find_spec('agents.migration') is None
assert 'ModelRequestLimitExceeded' not in dir(agents)
assert agents.Agent is agents.Agent
for removed in ("Runner", "ValidationIssue", "ValidationReport"):
    assert removed not in agents.__all__
    assert removed not in dir(agents)
    try:
        exec("from agents import " + removed)
    except ImportError:
        pass
    else:
        raise AssertionError(removed + " is still importable")
""",
        ],
        check=True,
    )


@pytest.mark.parametrize("entry", ["session", "agent", "agent_sync"])
async def test_retired_request_limit_cannot_silently_configure_a_run(entry: str):
    agent = Agent(instructions="Answer.")
    options: dict[str, Any] = {"max_model_requests": 0}
    with pytest.raises(TypeError, match="max_model_requests"):
        if entry == "session":
            async with agent.session(**options):
                pytest.fail("A retired request cap opened a session")
        elif entry == "agent":
            await agent.run("Answer", **options)
        else:
            await asyncio.to_thread(agent.run_sync, "Answer", **options)


def test_retired_name_fails_and_current_name_validates():
    options: dict[str, Any] = {"code_mode": True}
    with pytest.raises(TypeError, match="code_mode"):
        Agent(instructions="Answer.", **options)
    assert Agent(instructions="Answer.", programmatic_tool_calling=True).programmatic_tool_calling
    assert not Agent(instructions="Answer.").programmatic_tool_calling


@pytest.mark.parametrize("option", ["review_responses", "response_review_scope"])
def test_speculative_response_review_is_not_a_constructor_option(option):
    with pytest.raises(TypeError, match=option):
        Agent(instructions="Answer.", **{option: True})


@pytest.mark.parametrize(
    "field", ["root_turn_budget", "root_model_requests", "unrecognized_permission"]
)
def test_checkpoint_rejects_retired_or_unknown_fields(field):
    checkpoint = {
        "format_version": 2,
        "codex_revision": "test",
        "source_session_id": "root",
        "history": [],
        "root_metadata": {},
        field: {"limit": 1},
    }
    with pytest.raises(ValidationError, match=field):
        SessionState.model_validate(checkpoint)
