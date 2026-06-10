"""ModelProvider that routes OCI Generative AI model names to the right endpoint.

OCI Generative AI serves its catalog over two OpenAI-compatible endpoints:

- Chat completions: the default for the on-demand catalog (`openai.*` and most
  other model IDs).
- Responses: required for Responses-only reasoning models. These cannot be
  detected from the model name, so select them with the `responses:` prefix
  (e.g. `"responses:openai.gpt-5"`).
"""

from __future__ import annotations

from openai import AsyncOpenAI

from ...exceptions import UserError
from ...models.interface import Model, ModelProvider
from .oci_model import (
    DEFAULT_REQUEST_TIMEOUT,
    OCIChatCompletionsModel,
    OCIResponsesModel,
    build_signed_openai_client,
)
from .oci_signer import OCIAuthType, OCIClientConfig, resolve_client_config

_RESPONSES_PREFIX = "responses:"


class OCIProvider(ModelProvider):
    """A ModelProvider for the OCI Generative AI service. You can use it via:

    ```python
    Runner.run(agent, input, run_config=RunConfig(model_provider=OCIProvider()))
    ```

    Credentials are resolved from the standard OCI configuration sources: an
    `~/.oci/config` profile (API key or session token) or, when requested explicitly,
    instance/resource principals. The compartment used for inference is taken from the
    `compartment_id` argument or the `OCI_COMPARTMENT_ID` environment variable.
    """

    def __init__(
        self,
        *,
        auth_type: OCIAuthType | None = None,
        profile: str | None = None,
        config_file: str | None = None,
        region: str | None = None,
        compartment_id: str | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        project_id: str | None = None,
    ) -> None:
        self._auth_type = auth_type
        self._profile = profile
        self._config_file = config_file
        self._region = region
        self._compartment_id = compartment_id
        self._request_timeout = request_timeout
        self._project_id = project_id
        self._client_config: OCIClientConfig | None = None
        self._openai_client: AsyncOpenAI | None = None

    def _get_openai_client(self) -> AsyncOpenAI:
        # The signed client is shared by every model handed out by this provider.
        if self._openai_client is None:
            if self._client_config is None:
                self._client_config = resolve_client_config(
                    auth_type=self._auth_type,
                    profile=self._profile,
                    config_file=self._config_file,
                    region=self._region,
                    compartment_id=self._compartment_id,
                )
            self._openai_client = build_signed_openai_client(
                self._client_config,
                request_timeout=self._request_timeout,
                project_id=self._project_id,
            )
        return self._openai_client

    def get_model(self, model_name: str | None) -> Model:
        if not model_name:
            raise UserError(
                "OCIProvider requires an explicit model name (e.g. 'openai.gpt-4o' or "
                "'openai.gpt-5')."
            )

        if model_name.startswith(_RESPONSES_PREFIX):
            return OCIResponsesModel(
                model_name.removeprefix(_RESPONSES_PREFIX),
                openai_client=self._get_openai_client(),
            )
        return OCIChatCompletionsModel(model_name, openai_client=self._get_openai_client())

    async def aclose(self) -> None:
        if self._openai_client is not None:
            await self._openai_client.close()
            self._openai_client = None


__all__ = ["OCIProvider"]
