from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from pydantic import BaseModel

from ..exceptions import UserError
from ..model_settings import ModelSettings
from ..version import __version__
from .openai_agent_registration import resolve_openai_harness_id_for_model_provider

_ENV_RUNTIME_AUTH_ENABLED = "OPENAI_AGENT_RUNTIME_AUTH_ENABLED"
_ENV_AGENT_VERSION = "OPENAI_AGENT_VERSION"
_ENV_AGENT_RUNNING_LOCATION = "OPENAI_AGENT_RUNNING_LOCATION"
_ENV_AGENT_CAPABILITIES = "OPENAI_AGENT_CAPABILITIES"
_ENV_AGENT_RUNTIME_TTL = "OPENAI_AGENT_RUNTIME_TTL"
_DEFAULT_AGENT_HARNESS_ID = "agents-sdk-python"
_DEFAULT_RUNNING_LOCATION = "client"
_DEFAULT_CAPABILITIES = ("responsesapi",)
_AGENT_ASSERTION_SCHEME = "AgentAssertion"


@dataclass(frozen=True)
class OpenAIAgentRuntimeAuthConfig:
    """Opt-in configuration for verified sandbox runtime attribution on Responses calls."""

    agent_harness_id: str | None = None
    """Stable registry or interface identifier for the agent runtime."""

    agent_version: str | None = None
    """Version of the running agent. Defaults to the installed Agents SDK version."""

    running_location: str | None = None
    """Logical location where the agent is running."""

    capabilities: Sequence[str] | None = None
    """Capabilities authorized for this agent runtime registration."""

    ttl: int | None = None
    """Optional runtime identity TTL in whole seconds."""

    external_task_ref: str | None = None
    """Optional caller-supplied task reference used to resolve a durable task id."""

    enabled: bool = True
    """Whether runtime auth should be enabled for this configuration."""


@dataclass(frozen=True)
class ResolvedOpenAIAgentRuntimeAuthConfig:
    agent_harness_id: str
    agent_version: str
    running_location: str
    capabilities: tuple[str, ...]
    ttl: int | None
    external_task_ref: str | None


class _RegisterAgentResponse(BaseModel):
    agent_runtime_id: str


class _RegisterTaskResponse(BaseModel):
    encrypted_task_id: str


class _OpenAIClientWithPost(Protocol):
    async def post(
        self,
        path: str,
        *,
        cast_to: type[Any],
        body: object | None = None,
        **kwargs: Any,
    ) -> Any: ...


def resolve_openai_agent_runtime_auth_config(
    config: OpenAIAgentRuntimeAuthConfig | None,
    *,
    model_provider: object,
) -> ResolvedOpenAIAgentRuntimeAuthConfig | None:
    if config is None and not _env_bool(_ENV_RUNTIME_AUTH_ENABLED):
        return None
    if config is not None and not config.enabled:
        return None

    agent_harness_id = _first_non_empty(
        config.agent_harness_id if config is not None else None,
        resolve_openai_harness_id_for_model_provider(model_provider),
        _DEFAULT_AGENT_HARNESS_ID,
    )
    agent_version = _first_non_empty(
        config.agent_version if config is not None else None,
        os.getenv(_ENV_AGENT_VERSION),
        __version__,
    )
    running_location = _first_non_empty(
        config.running_location if config is not None else None,
        os.getenv(_ENV_AGENT_RUNNING_LOCATION),
        _DEFAULT_RUNNING_LOCATION,
    )
    capabilities = _resolve_capabilities(config.capabilities if config is not None else None)
    ttl = (
        config.ttl
        if config is not None and config.ttl is not None
        else _env_int(_ENV_AGENT_RUNTIME_TTL)
    )
    external_task_ref = (
        _normalize_str(config.external_task_ref)
        if config is not None and config.external_task_ref is not None
        else None
    )

    return ResolvedOpenAIAgentRuntimeAuthConfig(
        agent_harness_id=agent_harness_id,
        agent_version=agent_version,
        running_location=running_location,
        capabilities=capabilities,
        ttl=ttl,
        external_task_ref=external_task_ref,
    )


class OpenAIAgentRuntimeAuthManager:
    def __init__(self, config: ResolvedOpenAIAgentRuntimeAuthConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._private_key: Any | None = None
        self._agent_public_key: str | None = None
        self._agent_runtime_id: str | None = None
        self._task_id: str | None = None

    async def authorization_header(self, client: _OpenAIClientWithPost) -> str:
        await self._ensure_registered(client)
        if self._private_key is None or self._agent_runtime_id is None or self._task_id is None:
            raise UserError("OpenAI agent runtime auth registration did not complete.")

        timestamp = _utc_timestamp()
        signature = _sign_agent_assertion(
            private_key=self._private_key,
            agent_runtime_id=self._agent_runtime_id,
            task_id=self._task_id,
            timestamp=timestamp,
        )
        assertion = _serialize_agent_assertion(
            agent_runtime_id=self._agent_runtime_id,
            task_id=self._task_id,
            timestamp=timestamp,
            signature=signature,
        )
        return f"{_AGENT_ASSERTION_SCHEME} {assertion}"

    async def _ensure_registered(self, client: _OpenAIClientWithPost) -> None:
        if self._agent_runtime_id is not None and self._task_id is not None:
            return

        async with self._lock:
            if self._agent_runtime_id is not None and self._task_id is not None:
                return

            private_key, agent_public_key = _generate_agent_keypair()
            register_agent_body: dict[str, object] = {
                "abom": {
                    "agent_version": self._config.agent_version,
                    "agent_harness_id": self._config.agent_harness_id,
                    "running_location": self._config.running_location,
                },
                "agent_public_key": agent_public_key,
                "capabilities": list(self._config.capabilities),
            }
            if self._config.ttl is not None:
                register_agent_body["ttl"] = self._config.ttl

            agent_response = await client.post(
                "/agent/register",
                cast_to=_RegisterAgentResponse,
                body=register_agent_body,
            )
            agent_runtime_id = agent_response.agent_runtime_id

            timestamp = _utc_timestamp()
            task_body: dict[str, str] = {
                "timestamp": timestamp,
                "signature": _sign_task_registration(
                    private_key=private_key,
                    agent_runtime_id=agent_runtime_id,
                    timestamp=timestamp,
                ),
            }
            if self._config.external_task_ref is not None:
                task_body["external_task_ref"] = self._config.external_task_ref

            task_response = await client.post(
                f"/agent/{agent_runtime_id}/task/register",
                cast_to=_RegisterTaskResponse,
                body=task_body,
            )
            task_id = _decrypt_task_id(
                encrypted_task_id=task_response.encrypted_task_id,
                private_key=private_key,
            )

            self._private_key = private_key
            self._agent_public_key = agent_public_key
            self._agent_runtime_id = agent_runtime_id
            self._task_id = task_id


def add_agent_assertion_header(
    model_settings: ModelSettings,
    *,
    authorization_header: str,
) -> ModelSettings:
    extra_headers = dict(model_settings.extra_headers or {})
    for header_name in extra_headers:
        if header_name.lower() == "authorization":
            raise UserError(
                "Sandbox agent runtime auth cannot be combined with an explicit Authorization "
                "header in ModelSettings.extra_headers."
            )
    extra_headers["Authorization"] = authorization_header
    return replace(model_settings, extra_headers=extra_headers)


def _first_non_empty(*values: str | None) -> str:
    for value in values:
        normalized = _normalize_str(value)
        if normalized is not None:
            return normalized
    raise UserError("Expected at least one non-empty value.")


def _normalize_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _env_bool(name: str) -> bool:
    value = os.getenv(name)
    return value is not None and value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _env_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise UserError(f"{name} must be an integer.") from exc


def _resolve_capabilities(configured: Sequence[str] | None) -> tuple[str, ...]:
    values: Sequence[str]
    if configured is not None:
        values = configured
    else:
        env_value = os.getenv(_ENV_AGENT_CAPABILITIES)
        values = env_value.split(",") if env_value else _DEFAULT_CAPABILITIES

    capabilities = tuple(normalized for value in values if (normalized := _normalize_str(value)))
    return capabilities or _DEFAULT_CAPABILITIES


def _utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _serialize_agent_assertion(
    *,
    agent_runtime_id: str,
    task_id: str,
    timestamp: str,
    signature: str,
) -> str:
    payload = json.dumps(
        {
            "agent_runtime_id": agent_runtime_id,
            "task_id": task_id,
            "timestamp": timestamp,
            "signature": signature,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return _urlsafe_b64encode(payload)


def _generate_agent_keypair() -> tuple[Any, str]:
    serialization, Ed25519PrivateKey = _cryptography()
    private_key = Ed25519PrivateKey.generate()
    agent_public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    return private_key, agent_public_key.decode("utf-8")


def _sign_payload(*, private_key: Any, payload: bytes) -> str:
    return base64.b64encode(private_key.sign(payload)).decode("ascii")


def _sign_task_registration(*, private_key: Any, agent_runtime_id: str, timestamp: str) -> str:
    return _sign_payload(
        private_key=private_key,
        payload=f"{agent_runtime_id}:{timestamp}".encode(),
    )


def _sign_agent_assertion(
    *,
    private_key: Any,
    agent_runtime_id: str,
    task_id: str,
    timestamp: str,
) -> str:
    return _sign_payload(
        private_key=private_key,
        payload=f"{agent_runtime_id}:{task_id}:{timestamp}".encode(),
    )


def _decrypt_task_id(*, encrypted_task_id: str, private_key: Any) -> str:
    serialization, _ = _cryptography()
    crypto_sign_ed25519_sk_to_curve25519, PrivateKey, SealedBox = _pynacl()
    private_seed = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    curve25519_private_key = PrivateKey(
        crypto_sign_ed25519_sk_to_curve25519(private_seed + public_key)
    )
    plaintext: bytes = SealedBox(curve25519_private_key).decrypt(
        base64.b64decode(encrypted_task_id)
    )
    return plaintext.decode("utf-8")


def _cryptography() -> tuple[Any, Any]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise UserError(
            "Sandbox agent runtime auth requires cryptography. Install the encrypt extra with "
            "`pip install 'openai-agents[encrypt]'`."
        ) from exc
    return serialization, Ed25519PrivateKey


def _pynacl() -> tuple[Any, Any, Any]:
    try:
        from nacl.bindings import crypto_sign_ed25519_sk_to_curve25519
        from nacl.public import PrivateKey, SealedBox
    except ImportError as exc:
        raise UserError(
            "Sandbox agent runtime auth requires PyNaCl. Install the encrypt extra with "
            "`pip install 'openai-agents[encrypt]'`."
        ) from exc
    return crypto_sign_ed25519_sk_to_curve25519, PrivateKey, SealedBox
