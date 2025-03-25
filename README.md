# OpenAPI Agents SDK

OpenAI's Agents SDK is really cool, but it only supports OpenAI's own API. Let's fix that! This project aims to extend the original framework to support multiple API providers, truly making it an "Open" AI Agents platform.

> This project builds upon and extends the [original OpenAI Agents SDK](https://github.com/openai/openai-agents-python/blob/main/README.md).


## Project Vision

The OpenAPI Agents SDK is a powerful framework designed to achieve an ambitious goal: **enabling any API that conforms to the OpenAPI specification to be utilized by intelligent agents**. Through this framework, we can:

- **Unified Integration**: Bring diverse APIs into the Agent framework
- **Intelligent Interaction**: Enable AI models to understand API capabilities and limitations, automatically calling appropriate endpoints
- **Multi-Model Support**: Support OpenAI, local models, and third-party AI services
- **Process Orchestration**: Solve complex problems through multi-Agent collaboration

Our extensions make this framework no longer limited to OpenAI's cloud-based models, but support various local and third-party models, providing broader possibilities for API integration.

## Current Progress

We have tested integration with multiple API providers:

**Azure OpenAI API** integration has been implemented perfectly. Most examples run successfully with the Azure API.
Only the Responses API examples fail, as Azure API doesn't currently support the responses API.

We've also implemented integration with **Ollama**, with some basic examples running successfully using the Ollama API. However, some examples (like handoffs) fail when using the Ollama API.
Other advanced examples are too challenging for the Ollama API to pass.

### Examples passed with Ollama
examples_OpenAPI/ollama/basic/hello_world.py
examples_OpenAPI/ollama/basic/hello_world_jupyter.py
examples_OpenAPI/ollama/basic/hello_world.py
examples_OpenAPI/ollama/basic/failed/agent_lifecycle_example.py
### Examples failed with Ollama
examples_OpenAPI/ollama/basic/failed/lifecycle_example.py

## Core Concepts

1. [**Agents**](https://openai.github.io/openai-agents-python/agents): LLMs configured with instructions, tools, guardrails, and handoffs
2. [**Handoffs**](https://openai.github.io/openai-agents-python/handoffs/): A specialized tool call used by the Agents SDK for transferring control between agents
3. [**Guardrails**](https://openai.github.io/openai-agents-python/guardrails/): Configurable safety checks for input and output validation
4. [**Tracing**](https://openai.github.io/openai-agents-python/tracing/): Built-in tracking of agent runs, allowing you to view, debug and optimize your workflows
5. [**Model Providers**](https://openai.github.io/openai-agents-python/ollama_integration): Support for multiple model providers including OpenAI and Ollama
## Using Azure OpenAI Service

Azure OpenAI provides enterprise-grade security and compliance features. Integrating with Azure is simple:

```python
import asyncio
import sys, os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from src.agents import Agent, Runner
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

async def main():
    # Create Azure OpenAI settings
    azure_settings = ModelSettings(
        provider="azure_openai",
        azure_endpoint="https://your-resource-name.openai.azure.com",
        azure_api_key="your-azure-api-key",
        azure_api_version="2024-02-15-preview",
        azure_deployment="your-deployment-name"
    )
    
    # Create run configuration
    run_config = RunConfig()
    run_config.model_provider = ModelProviderFactory.create_provider(azure_settings)

    # Create Agent instance
    agent = Agent(
        name="AzureAssistant",
        instructions="You are a helpful assistant running on Azure.",
        model_settings=azure_settings
    )
    
    # Run Agent
    result = await Runner.run(
        agent, 
        "Explain the advantages of using Azure OpenAI.", 
        run_config=run_config
    )
    
    # Print results
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

For more details, please refer to our [Azure Integration Documentation](docs/azure_integration.md).

## Using Local Ollama Models

This is our key extension. Using local Ollama models is straightforward:

```python
import asyncio
import sys, os

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from src.agents import Agent, Runner
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

async def main():
    # Create Ollama model settings
    ollama_settings = ModelSettings(
        provider="ollama",  # Specify Ollama as the provider
        ollama_base_url="http://localhost:11434",  # Ollama service address
        ollama_default_model="llama3.2",  # Use llama3.2 model
        temperature=0.7  # Optional: control creativity
    )
    
    # Create run configuration
    run_config = RunConfig()
    run_config.model_provider = ModelProviderFactory.create_provider(ollama_settings)

    # Create Agent instance
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant.",
        model_settings=ollama_settings  # Use Ollama settings
    )
    
    # Run Agent
    result = await Runner.run(
        agent, 
        "Write a Python function to calculate the Fibonacci sequence.", 
        run_config=run_config
    )
    
    # Print results
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

For more details, please refer to our [Ollama Integration Documentation](docs/ollama_integration.md).


## Getting Started

1. Set up your Python environment

```
python -m venv env
source env/bin/activate
```

2. Install the Agents SDK

```
pip install -e .
```

For Ollama support, ensure you have Ollama installed and running:
1. Download and install Ollama from [ollama.ai](https://ollama.ai)
2. Run `ollama serve` to start the service
3. Pull the required model, e.g., `ollama pull llama3.2`

## Project Structure

- **`src/`**: Core source code
  - **`agents/`**: Main implementation of the Agent framework
  - **`agents/models/`**: Implementations of different model providers
- **`examples_OpenAPI/`**: OpenAPI integration examples
  - **`ollama/`**: Ollama integration examples
  - **`azure/`**: Azure OpenAI integration examples
- **`docs/`**: Project documentation
  - **`ollama_integration.md`**: Detailed documentation on Ollama integration
  - **`azure_integration.md`**: Detailed documentation on Azure integration
  - **`ollama_API/`**: Ollama API reference

## Future Plans

We plan to continue expanding this framework to support more APIs and model providers:

- **Next immediate steps**:
  - **Azure AI Integration**: Add support for Azure OpenAI Service to enable enterprise-grade AI capabilities with Azure's security and compliance features
  - **AWS Bedrock Integration**: Implement integration with AWS Bedrock to support models like Claude, Titan, and others in the AWS ecosystem

- Additional roadmap items:
  - Add automatic integration for more OpenAPI specification APIs
  - Implement streaming response support
  - Improve tool call compatibility across different models
  - Add more model providers (e.g., Anthropic, Gemini, etc.)

## Contribution Guidelines

We welcome and encourage community contributions to help make OpenAI Agent truly "Open"! Here's how you can participate:

### How to Contribute

1. **Fork and Clone the Repository**
   ```bash
   git clone https://github.com/your-username/openAPI-agents-python.git
   cd openAPI-agents-python
   ```

2. **Create a New Branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Implement Your Changes**
   - Add support for new API providers
   - Fix issues in existing integrations
   - Improve documentation
   - Add more examples

4. **Test Your Changes**
   - Ensure existing tests pass
   - Add tests for new features

5. **Commit and Push Your Changes**
   ```bash
   git commit -m "Add support for XYZ API provider"
   git push origin feature/your-feature-name
   ```

6. **Create a Pull Request**
   - Provide a clear PR description
   - Explain what problem your changes solve

### Contribution Areas

You can contribute in the following areas:

1. **New API Provider Support**
   - Add support for Anthropic, Gemini, Cohere, etc.
   - Implement cloud service provider integrations like AWS Bedrock, Google Vertex AI

2. **Existing Integration Improvements**
   - Improve Ollama integration compatibility
   - Optimize Azure OpenAI integration performance

3. **Documentation Enhancement**
   - Write more detailed API integration guides
   - Create more examples and tutorials
   - Translate documentation

4. **Bug Fixes and Feature Requests**
   - Report issues or suggest new features
   - Fix existing issues

We believe that through community effort, we can build a truly open Agent framework that allows any API to leverage the intelligent capabilities of Agent technology.

## Acknowledgements

We would like to thank the open-source community for their outstanding work, especially:

- [OpenAI](https://openai.com/) (original Agents SDK)
- [Ollama](https://ollama.ai/) (local LLM runtime framework)
- [Pydantic](https://docs.pydantic.dev/latest/) (data validation) and [PydanticAI](https://ai.pydantic.dev/) (advanced agent framework)
- [MkDocs](https://github.com/squidfunk/mkdocs-material)
- [Griffe](https://github.com/mkdocstrings/griffe)

Join us in making OpenAI Agent more "Open"!
