"""Models for the OCI Generative AI OpenAI-compatible endpoints.

OCI Generative AI exposes most of its hosted catalog (including the `openai.*`
model IDs) on OpenAI-compatible `chat/completions` and `responses` endpoints,
authenticated with OCI request signing instead of bearer tokens. The classes
here reuse the SDK's OpenAI model implementations against those endpoints,
connecting through Oracle's official `oci-openai` client, which performs the
request signing and compartment routing.
"""

from __future__ import annotations

import os
from typing import Any, Literal, cast

import httpx
from openai import AsyncOpenAI

from ...exceptions import UserError
from ...models.openai_chatcompletions import OpenAIChatCompletionsModel
from ...models.openai_responses import OpenAIResponsesModel

try:
    from oci_openai import (
        AsyncOciOpenAI,
        OciInstancePrincipalAuth,
        OciResourcePrincipalAuth,
        OciSessionAuth,
        OciUserPrincipalAuth,
    )
except ImportError as _e:
    raise ImportError(
        "`oci-openai` is required to use the OCI model classes. You can install it via the "
        "optional dependency group: `pip install 'openai-agents[oci]'`."
    ) from _e

DEFAULT_OCI_REGION = "us-chicago-1"
"""Fallback region used when none is configured anywhere else."""

# Reasoning models can take minutes before the first byte; use a generous default.
DEFAULT_REQUEST_TIMEOUT = 300.0

OCIAuthType = Literal["api_key", "security_token", "instance_principal", "resource_principal"]

_DEFAULT_CONFIG_FILE = "~/.oci/config"
_DEFAULT_PROFILE = "DEFAULT"


def _load_profile(profile: str | None, config_file: str | None) -> dict[str, Any]:
    import oci

    config: dict[str, Any] = oci.config.from_file(
        file_location=config_file or _DEFAULT_CONFIG_FILE,
        profile_name=profile or os.environ.get("OCI_CLI_PROFILE") or _DEFAULT_PROFILE,
    )
    return config


def _build_auth(
    auth_type: OCIAuthType | None,
    profile: str | None,
    config_file: str | None,
    profile_config: dict[str, Any],
) -> httpx.Auth:
    """Select the `oci-openai` auth implementation for the requested auth mode.

    When `auth_type` is omitted, profiles carrying a `security_token_file` use
    session-token auth and everything else uses API-key auth. Principal-based modes
    must be requested explicitly because they cannot be detected from a config file.
    """
    if auth_type == "instance_principal":
        return cast(httpx.Auth, OciInstancePrincipalAuth())
    if auth_type == "resource_principal":
        return cast(httpx.Auth, OciResourcePrincipalAuth())

    resolved_config_file = config_file or _DEFAULT_CONFIG_FILE
    resolved_profile = profile or os.environ.get("OCI_CLI_PROFILE") or _DEFAULT_PROFILE
    use_session_token = auth_type == "security_token" or (
        auth_type is None and profile_config.get("security_token_file")
    )
    if use_session_token:
        return cast(
            httpx.Auth,
            OciSessionAuth(config_file=resolved_config_file, profile_name=resolved_profile),
        )
    return cast(
        httpx.Auth,
        OciUserPrincipalAuth(config_file=resolved_config_file, profile_name=resolved_profile),
    )


def build_signed_openai_client(
    *,
    auth_type: OCIAuthType | None = None,
    profile: str | None = None,
    config_file: str | None = None,
    region: str | None = None,
    compartment_id: str | None = None,
    project_id: str | None = None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> AsyncOpenAI:
    """Build an `AsyncOciOpenAI` client wired to an OCI Generative AI regional endpoint.

    The returned client (a drop-in `AsyncOpenAI` subclass from Oracle's `oci-openai`
    package) signs every request with the resolved OCI credentials and attaches the
    compartment header the service requires.

    Resolution order for the region: explicit argument, `OCI_REGION` env var, the
    config file profile's `region`, then the service default. For the compartment:
    explicit argument, `OCI_COMPARTMENT_ID` env var, then the profile's tenancy as a
    best-effort fallback.

    Args:
        auth_type: OCI auth mode; inferred from the config profile when omitted.
        profile: OCI config profile name (defaults to `OCI_CLI_PROFILE` or `DEFAULT`).
        config_file: OCI config file location (defaults to `~/.oci/config`).
        region: OCI region whose Generative AI endpoint should be called.
        compartment_id: Compartment all inference requests are billed against.
        project_id: Optional OCI Generative AI project OCID
            (`ocid1.generativeaiproject...`), sent as the `OpenAI-Project` header.
            Projects scope response/conversation retention and memory settings on the
            Responses endpoint.
        request_timeout: Per-request timeout in seconds.
    """
    uses_file_config = auth_type not in ("instance_principal", "resource_principal")
    profile_config = _load_profile(profile, config_file) if uses_file_config else {}

    auth = _build_auth(auth_type, profile, config_file, profile_config)
    resolved_region = (
        region or os.environ.get("OCI_REGION") or profile_config.get("region") or DEFAULT_OCI_REGION
    )
    resolved_compartment = (
        compartment_id or os.environ.get("OCI_COMPARTMENT_ID") or profile_config.get("tenancy")
    )
    if not resolved_compartment:
        raise UserError(
            "A compartment_id is required for OCI Generative AI. Pass it explicitly or set "
            "the OCI_COMPARTMENT_ID environment variable."
        )

    return cast(
        AsyncOpenAI,
        AsyncOciOpenAI(
            auth=auth,
            region=str(resolved_region),
            compartment_id=resolved_compartment,
            timeout=request_timeout,
            project=project_id,
        ),
    )


class OCIChatCompletionsModel(OpenAIChatCompletionsModel):
    """OCI Generative AI model served over the OpenAI-compatible chat completions API.

    This is the right transport for `openai.*` model IDs and most of the rest of the
    on-demand catalog.

    Example:
        ```python
        model = OCIChatCompletionsModel(
            "openai.gpt-4o",
            compartment_id="ocid1.compartment.oc1..example",
        )
        agent = Agent(name="Assistant", model=model)
        ```
    """

    def __init__(
        self,
        model: str,
        *,
        auth_type: OCIAuthType | None = None,
        profile: str | None = None,
        config_file: str | None = None,
        region: str | None = None,
        compartment_id: str | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        openai_client: AsyncOpenAI | None = None,
        project_id: str | None = None,
    ) -> None:
        owns_openai_client = openai_client is None
        if openai_client is None:
            openai_client = build_signed_openai_client(
                auth_type=auth_type,
                profile=profile,
                config_file=config_file,
                region=region,
                compartment_id=compartment_id,
                project_id=project_id,
                request_timeout=request_timeout,
            )
        super().__init__(model, openai_client)
        self._owns_openai_client = owns_openai_client

    async def close(self) -> None:
        """Release the internally created signing client, if this model owns it."""
        await super().close()
        if self._owns_openai_client:
            await self._client.close()


class OCIResponsesModel(OpenAIResponsesModel):
    """OCI Generative AI model served over the OpenAI-compatible Responses API.

    Required for Responses-only reasoning models in the OCI catalog. The transport is
    server-stateful: multi-turn continuation uses `previous_response_id`, which the
    runner manages. For tenancies with Zero Data Retention enabled, pass
    `ModelSettings(store=False)` so the full history is sent each turn instead.

    Example:
        ```python
        model = OCIResponsesModel(
            "openai.gpt-5",
            compartment_id="ocid1.compartment.oc1..example",
        )
        agent = Agent(name="Assistant", model=model)
        ```
    """

    def __init__(
        self,
        model: str,
        *,
        auth_type: OCIAuthType | None = None,
        profile: str | None = None,
        config_file: str | None = None,
        region: str | None = None,
        compartment_id: str | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        openai_client: AsyncOpenAI | None = None,
        project_id: str | None = None,
    ) -> None:
        owns_openai_client = openai_client is None
        if openai_client is None:
            openai_client = build_signed_openai_client(
                auth_type=auth_type,
                profile=profile,
                config_file=config_file,
                region=region,
                compartment_id=compartment_id,
                project_id=project_id,
                request_timeout=request_timeout,
            )
        super().__init__(model, openai_client)
        self._owns_openai_client = owns_openai_client

    async def close(self) -> None:
        """Release the internally created signing client, if this model owns it."""
        await super().close()
        if self._owns_openai_client:
            await self._client.close()


__all__ = [
    "DEFAULT_OCI_REGION",
    "DEFAULT_REQUEST_TIMEOUT",
    "OCIAuthType",
    "OCIChatCompletionsModel",
    "OCIResponsesModel",
    "build_signed_openai_client",
]
