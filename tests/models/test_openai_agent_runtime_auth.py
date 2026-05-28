from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from nacl.bindings import crypto_sign_ed25519_pk_to_curve25519
from nacl.public import PublicKey, SealedBox

from agents import (
    ModelSettings,
    OpenAIAgentRuntimeAuthConfig,
    OpenAIResponsesModel,
    OpenAIResponsesWSModel,
    RunConfig,
)
from agents.exceptions import UserError
from agents.models.openai_agent_runtime_auth import (
    OpenAIAgentRuntimeAuthManager,
    add_agent_assertion_header,
    resolve_openai_agent_runtime_auth_config,
)
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.runtime import SandboxRuntime


class _FakeOpenAIClient:
    def __init__(self, *, task_id: str = "task_456") -> None:
        self.task_id = task_id
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.agent_public_key: str | None = None
        self.base_url = httpx.URL("https://api.openai.com/v1/")
        self.websocket_base_url = None
        self.default_headers = {"Authorization": "Bearer sk-test", "X-Client": "1"}
        self.default_query: dict[str, str] = {}

    async def post(
        self,
        path: str,
        *,
        cast_to: type[Any],
        body: object | None = None,
        **kwargs: Any,
    ) -> Any:
        _ = kwargs
        assert isinstance(body, dict)
        self.calls.append((path, body))
        if path == "/agent/register":
            self.agent_public_key = str(body["agent_public_key"])
            return cast_to.model_validate({"agent_runtime_id": "agent_123"})
        if path == "/agent/agent_123/task/register":
            assert self.agent_public_key is not None
            return cast_to.model_validate(
                {"encrypted_task_id": _encrypt_task_id(self.task_id, self.agent_public_key)}
            )
        raise AssertionError(f"Unexpected path: {path}")


def test_runtime_auth_config_is_env_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_AGENT_RUNTIME_AUTH_ENABLED", raising=False)

    assert resolve_openai_agent_runtime_auth_config(None, model_provider=object()) is None

    monkeypatch.setenv("OPENAI_AGENT_RUNTIME_AUTH_ENABLED", "true")
    resolved = resolve_openai_agent_runtime_auth_config(None, model_provider=object())

    assert resolved is not None
    assert resolved.agent_harness_id == "agents-sdk-python"
    assert resolved.running_location == "client"
    assert resolved.capabilities == ("responsesapi",)


@pytest.mark.asyncio
async def test_runtime_auth_manager_registers_runtime_task_and_builds_assertion() -> None:
    resolved = resolve_openai_agent_runtime_auth_config(
        OpenAIAgentRuntimeAuthConfig(
            agent_harness_id="test-harness",
            agent_version="1.2.3",
            running_location="test-location",
            capabilities=("responsesapi", "sandbox"),
            ttl=3600,
            external_task_ref="external-run",
        ),
        model_provider=object(),
    )
    assert resolved is not None
    client = _FakeOpenAIClient()
    manager = OpenAIAgentRuntimeAuthManager(resolved)

    header = await manager.authorization_header(client)

    assert len(client.calls) == 2
    register_path, register_body = client.calls[0]
    assert register_path == "/agent/register"
    assert register_body["abom"] == {
        "agent_version": "1.2.3",
        "agent_harness_id": "test-harness",
        "running_location": "test-location",
    }
    assert register_body["capabilities"] == ["responsesapi", "sandbox"]
    assert register_body["ttl"] == 3600
    task_path, task_body = client.calls[1]
    assert task_path == "/agent/agent_123/task/register"
    assert task_body["external_task_ref"] == "external-run"

    payload = _decode_assertion(header)
    assert payload["agent_runtime_id"] == "agent_123"
    assert payload["task_id"] == "task_456"
    assert client.agent_public_key is not None
    _verify_agent_assertion(payload, client.agent_public_key)


@pytest.mark.asyncio
async def test_sandbox_runtime_adds_agent_assertion_header_for_responses_model() -> None:
    client = _FakeOpenAIClient()
    model = OpenAIResponsesModel(model="gpt-4.1", openai_client=client)  # type: ignore[arg-type]
    sandbox_agent = SandboxAgent(name="sandbox")
    runtime = SandboxRuntime(
        starting_agent=sandbox_agent,
        run_config=RunConfig(
            sandbox=SandboxRunConfig(
                agent_runtime_auth=OpenAIAgentRuntimeAuthConfig(
                    agent_harness_id="test-harness",
                    running_location="test-location",
                )
            )
        ),
        run_state=None,
    )

    updated = await runtime.prepare_model_settings(
        public_agent=sandbox_agent,
        model=model,
        model_settings=ModelSettings(extra_headers={"X-Test": "1"}),
    )

    assert updated.extra_headers is not None
    assert updated.extra_headers["X-Test"] == "1"
    assert str(updated.extra_headers["Authorization"]).startswith("AgentAssertion ")
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_sandbox_runtime_adds_agent_assertion_header_for_responses_ws_model() -> None:
    client = _FakeOpenAIClient()
    model = OpenAIResponsesWSModel(model="gpt-4.1", openai_client=client)  # type: ignore[arg-type]
    sandbox_agent = SandboxAgent(name="sandbox")
    runtime = SandboxRuntime(
        starting_agent=sandbox_agent,
        run_config=RunConfig(
            sandbox=SandboxRunConfig(
                agent_runtime_auth=OpenAIAgentRuntimeAuthConfig(
                    agent_harness_id="test-harness",
                    running_location="test-location",
                )
            )
        ),
        run_state=None,
    )
    updated = await runtime.prepare_model_settings(
        public_agent=sandbox_agent,
        model=model,
        model_settings=ModelSettings(extra_headers={"X-Test": "1"}),
    )
    _, _, handshake_headers = await model._prepare_websocket_request(
        {
            "model": "gpt-4.1",
            "input": [],
            "extra_headers": updated.extra_headers,
        }
    )

    assert updated.extra_headers is not None
    assert updated.extra_headers["X-Test"] == "1"
    assert str(updated.extra_headers["Authorization"]).startswith("AgentAssertion ")
    assert handshake_headers["Authorization"].startswith("AgentAssertion ")
    assert handshake_headers["X-Client"] == "1"
    assert len(client.calls) == 2


def test_add_agent_assertion_header_rejects_explicit_authorization() -> None:
    with pytest.raises(UserError, match="explicit Authorization"):
        add_agent_assertion_header(
            ModelSettings(extra_headers={"authorization": "Bearer sk-test"}),
            authorization_header="AgentAssertion token",
        )


def _encrypt_task_id(task_id: str, agent_public_key: str) -> str:
    loaded_key = serialization.load_ssh_public_key(agent_public_key.encode("utf-8"))
    assert isinstance(loaded_key, Ed25519PublicKey)
    raw_public_key = loaded_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    curve25519_public_key = PublicKey(crypto_sign_ed25519_pk_to_curve25519(raw_public_key))
    encrypted_task_id = SealedBox(curve25519_public_key).encrypt(task_id.encode("utf-8"))
    return base64.b64encode(encrypted_task_id).decode("ascii")


def _decode_assertion(header: str) -> dict[str, str]:
    scheme, token = header.split(" ", 1)
    assert scheme == "AgentAssertion"
    padding = "=" * (-len(token) % 4)
    payload = json.loads(base64.urlsafe_b64decode(f"{token}{padding}").decode("utf-8"))
    assert isinstance(payload, dict)
    return {str(key): str(value) for key, value in payload.items()}


def _verify_agent_assertion(payload: dict[str, str], agent_public_key: str) -> None:
    loaded_key = serialization.load_ssh_public_key(agent_public_key.encode("utf-8"))
    assert isinstance(loaded_key, Ed25519PublicKey)
    loaded_key.verify(
        base64.b64decode(payload["signature"]),
        (f"{payload['agent_runtime_id']}:{payload['task_id']}:{payload['timestamp']}").encode(),
    )
