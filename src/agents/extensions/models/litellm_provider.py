from ...models.interface import Model, ModelProvider
from .litellm_model import LitellmModel

DEFAULT_MODEL: str = "gpt-4.1"


class LitellmProvider(ModelProvider):
    """A ModelProvider that uses LiteLLM to route to any model provider. You can use it via:
    ```python
    Runner.run(agent, input, run_config=RunConfig(model_provider=LitellmProvider()))
    ```
    See supported models here: [litellm models](https://docs.litellm.ai/docs/providers).

    NOTE: API keys must be set via environment variables. If you're using models that require
    additional configuration (e.g. Azure API base or version), those must also be set via the
    environment variables that LiteLLM expects. If you have more advanced needs, we recommend
    copy-pasting this class and making any modifications you need.
    """

    def get_model(self, model_name: str | None) -> Model:
        return LitellmModel(model_name or DEFAULT_MODEL)
