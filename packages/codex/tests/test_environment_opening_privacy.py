"""Failed native opening must release environment secrets from SDK traceback locals."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

import pytest

from agents import Agent, CustomStorage, Environment, NativeProvider, SQLiteSessionStore


def assert_private_opening(error: BaseException, environment: Environment, marker: str) -> None:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert marker not in str(current)
        for frame, _ in traceback.walk_tb(current.__traceback__):
            if not frame.f_globals["__name__"].startswith(("agents.", "_agents_codex.")):
                continue  # The application's own frame still owns its Environment.
            for name, value in frame.f_locals.items():
                assert value is not environment, (frame.f_code.co_name, name)
                assert marker not in repr(value), (frame.f_code.co_name, name)
            if frame.f_code.co_name == "_open":
                opening = frame.f_locals.get("opening")
                assert opening is None or opening.environment is None
        pending.extend(
            chained for chained in (current.__cause__, current.__context__) if chained is not None
        )


@pytest.mark.parametrize(
    "closed_provider", [False, True], ids=["native-selection", "provider-admission"]
)
async def test_failed_environment_opening_releases_secrets(
    tmp_path: Path, closed_provider: bool
) -> None:
    marker = "synthetic-environment-opening-token"
    environment = Environment.local(
        cwd=str(tmp_path / "missing"), env={"TOKEN": marker}, tools=["exec_command"]
    )
    provider = NativeProvider(api_key="synthetic-model-key")
    if closed_provider:
        await provider.aclose()
    expected = (
        "NativeProvider is closed"
        if closed_provider
        else "could not resolve an executor write root"
    )
    try:
        with pytest.raises(RuntimeError, match=expected) as caught:
            async with Agent(instructions="Answer.", provider=provider).session(
                environment=environment
            ):
                pytest.fail("Opened an invalid Environment or closed provider")
        assert_private_opening(caught.value, environment, marker)
        # Reporters can reach the original failure through either exception link.
        diagnostic = RuntimeError("Opening failed")
        diagnostic.__cause__ = caught.value
        diagnostic.__context__ = caught.value
        assert_private_opening(diagnostic, environment, marker)
        assert environment.env == {"TOKEN": marker}
    finally:
        await provider.aclose()


@pytest.mark.requires_native_linux_sandbox
async def test_checkpoint_policy_failure_releases_native_environment_payload(
    tmp_path: Path,
) -> None:
    marker = "synthetic-restored-environment-token"
    environment = Environment.local(
        cwd=str(tmp_path), env={"TOKEN": marker}, tools=["exec_command"]
    )
    provider = NativeProvider(api_key="synthetic-model-key")
    agent = Agent(instructions="Answer.", provider=provider)
    try:
        async with agent.session(environment=environment) as session:
            state = await session.save_state()
        state = state.model_copy(update={"executor_policy_digest": "0" * 64})
        with pytest.raises(ValueError, match="agree on the applied executor policy") as caught:
            async with agent.session(state=state, environment=environment):
                pytest.fail("Restored a checkpoint with an incompatible applied executor policy")
        assert_private_opening(caught.value, environment, marker)
        for frame, _ in traceback.walk_tb(caught.value.__traceback__):
            if frame.f_code.co_name == "_open":
                unpublished = frame.f_locals["session"]
                assert unpublished._executor is None
                assert frame.f_locals["opening"].lease is None
                assert frame.f_locals["lease"] is None
        assert environment.env == {"TOKEN": marker}
    finally:
        await provider.aclose()


@pytest.mark.parametrize("failure", ["instructions", "storage", "retention", "guidance"])
async def test_early_opening_releases_environment(tmp_path: Path, failure: str) -> None:
    marker = "synthetic-early-opening-token"
    environment = Environment.local(cwd=str(tmp_path), env={"TOKEN": marker})
    provider = NativeProvider(api_key="synthetic-model-key")
    options: dict[str, Any] = {}
    expected: type[Exception] = ValueError
    match: str | None = None
    if failure == "instructions":
        options["instruction_files"] = [tmp_path / "missing.md"]
        expected = FileNotFoundError
    elif failure == "storage":
        options["storage"] = CustomStorage(SQLiteSessionStore(tmp_path / "canonical.sqlite"))
        match = "CustomStorage preview does not support"
    elif failure == "retention":
        options["observation_retention"] = "invalid"
        match = "observation_retention"
    else:
        options["project_guidance"] = "invalid"
        expected = TypeError
        match = "project_guidance"
    try:
        with pytest.raises(expected, match=match) as caught:
            async with Agent(instructions="Answer.", provider=provider).session(
                environment=environment, **options
            ):
                pytest.fail("An invalid opening succeeded")
        assert_private_opening(caught.value, environment, marker)
        assert environment.env == {"TOKEN": marker}
    finally:
        await provider.aclose()
