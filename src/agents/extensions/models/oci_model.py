"""Models for the OCI Generative AI OpenAI-compatible transports.

OCI Generative AI exposes most of its hosted catalog (including the `openai.*`
model IDs) on OpenAI-compatible `chat/completions` and `responses` endpoints,
authenticated with OCI request signing instead of bearer tokens. The classes
here reuse the SDK's OpenAI model implementations against those endpoints by
injecting a signing HTTP client.
"""

from __future__ import annotations

import httpx
from openai import AsyncOpenAI

from ...models.openai_chatcompletions import OpenAIChatCompletionsModel
from ...models.openai_responses import OpenAIResponsesModel
from .oci_signer import (
    OCIAuthType,
    OCIClientConfig,
    OCIRequestSigner,
    oci_openai_base_url,
    resolve_client_config,
)

# Reasoning models can take minutes before the first byte; use a generous default.
DEFAULT_REQUEST_TIMEOUT = 300.0


def build_signed_openai_client(
    client_config: OCIClientConfig,
    *,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> AsyncOpenAI:
    """Build an `AsyncOpenAI` client wired to an OCI Generative AI regional endpoint.

    The returned client signs every request with the resolved OCI credentials and
    routes it to the region's OpenAI-compatible base URL. The `api_key` placeholder is
    never sent; the signer strips bearer auth before signing.
    """
    http_client = httpx.AsyncClient(
        auth=OCIRequestSigner(
            client_config.signer,
            compartment_id=client_config.compartment_id,
            refresh_signer=client_config.refresh_signer,
        ),
        timeout=httpx.Timeout(request_timeout),
    )
    return AsyncOpenAI(
        base_url=oci_openai_base_url(client_config.region),
        api_key="oci-request-signing",
        http_client=http_client,
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
    ) -> None:
        if openai_client is None:
            client_config = resolve_client_config(
                auth_type=auth_type,
                profile=profile,
                config_file=config_file,
                region=region,
                compartment_id=compartment_id,
            )
            openai_client = build_signed_openai_client(
                client_config, request_timeout=request_timeout
            )
        super().__init__(model, openai_client)


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
    ) -> None:
        if openai_client is None:
            client_config = resolve_client_config(
                auth_type=auth_type,
                profile=profile,
                config_file=config_file,
                region=region,
                compartment_id=compartment_id,
            )
            openai_client = build_signed_openai_client(
                client_config, request_timeout=request_timeout
            )
        super().__init__(model, openai_client)


__all__ = [
    "DEFAULT_REQUEST_TIMEOUT",
    "OCIChatCompletionsModel",
    "OCIResponsesModel",
    "build_signed_openai_client",
]
