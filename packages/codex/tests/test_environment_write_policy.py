"""Environment write defaults use the existing native enforcement contract."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from agents import Environment
from tests.native_fixtures import ResponsesFixture, responses as responses


def test_local_and_remote_default_to_strict(tmp_path: Path) -> None:
    remote = Environment(url="wss://executor.invalid", cwd="/workspace")
    local = Environment.local(cwd=tmp_path)
    for environment in (remote, local):
        assert environment.write_policy == "strict"
        assert environment.writable_roots == ()
        assert environment._to_native()["writable_roots"] == []
        with pytest.raises(FrozenInstanceError):
            environment.write_policy = "legacy"  # type: ignore[misc]
    assert (
        remote._to_native()
        == Environment(
            url="wss://executor.invalid", cwd="/workspace", writable_roots=[]
        )._to_native()
    )


def test_legacy_requires_explicit_remote_declaration(tmp_path: Path) -> None:
    legacy = Environment(url="wss://executor.invalid", cwd="/workspace", write_policy="legacy")
    assert legacy.write_policy == "legacy"
    assert "writable_roots" not in legacy._to_native()
    for roots in ([], ["/output"]):
        with pytest.raises(ValueError, match="Legacy write policy does not accept writable_roots"):
            Environment(
                url="wss://executor.invalid",
                cwd="/workspace",
                write_policy="legacy",
                writable_roots=roots,
            )
    with pytest.raises(ValueError, match="Legacy write policy requires a remote Environment"):
        Environment.local(cwd=tmp_path, write_policy="legacy")


@pytest.mark.parametrize("policy", [None, "", "unknown", False])
def test_invalid_write_policy_is_not_a_default(policy: Any) -> None:
    with pytest.raises(ValueError, match="write_policy must be strict or legacy"):
        Environment(url="wss://executor.invalid", cwd="/workspace", write_policy=policy)


@pytest.mark.parametrize(
    "mode", ["legacy-to-default", "implicit-to-default", "strict-to-legacy", "strict-to-none"]
)
async def test_checkpoint_mode_mismatch_precedes_native_acquisition(
    mode: str, monkeypatch: pytest.MonkeyPatch, responses: ResponsesFixture
) -> None:
    import hashlib

    from agents import Agent, _native
    from tests.test_native_agent import provider

    agent = Agent(instructions="Work.", provider=provider(responses))
    async with agent.session() as session:
        saved = await session.save_state()
    saved = saved.model_copy(
        update={
            "executor_policy_digest": (
                None
                if mode == "legacy-to-default"
                else hashlib.sha256(b"implicit-remote-workspace").hexdigest()
                if mode == "implicit-to-default"
                else "a" * 64
            )
        }
    )
    acquisitions = []

    def fail_acquisition(*args: Any, **kwargs: Any) -> Any:
        acquisitions.append(True)
        pytest.fail("Write-mode mismatch acquired a native engine")

    monkeypatch.setattr(_native, "create_engine", fail_acquisition)
    monkeypatch.setattr(_native, "create_checkpoint_engine", fail_acquisition)
    selected = (
        None
        if mode == "strict-to-none"
        else Environment(
            url="wss://executor.invalid",
            cwd="/workspace",
            write_policy="legacy" if mode == "strict-to-legacy" else "strict",
        )
    )
    with pytest.raises(ValueError, match="agree on the applied executor policy"):
        async with agent.session(state=saved, environment=selected):
            pytest.fail("Opened with a different write mode")
    assert not acquisitions
