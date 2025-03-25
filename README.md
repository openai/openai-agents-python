# OpenAPI Agents SDK

> This project builds upon and extends the [original OpenAI Agents SDK](https://github.com/openai/openai-agents-python/blob/main/README.md).

A example of using the OpenAPI Agents SDK to interact with the Ollama API:
```bash
(env) ➜  openAPI-agents-python git:(main) ✗ python examples_OpenAPI/ollama/basic/hello_world.py 
Running Agent, please wait...
Sending request to: http://localhost:11434/v1/chat/completions
Request payload: {"model": "llama3.2", "temperature": 0.7, "max_tokens": 1000, "messages": [{"role": "system", "content": "You only respond in haikus."}, {"content": "Tell me about recursion in programming.", "role": "user"}]}

Result:
A function calls self,  
Solving problems piece by piece,  
Ends when base is met.  

Stack grows with each call,  
Memory used till solution,  
Careful with limits.  

Tail recursion saves,  
Optimizing stack usage well,  
In some languages clear.  

Simple tasks repeated,  
Towers of Hanoi's magic dance,  
Math problems delight.  

Endless loops beware!  
Missing base case is your foe,  
Watch for infinite calls.  

---
```

## Project Vision

The OpenAPI Agents SDK is a powerful framework designed to achieve an ambitious goal: **enabling any API that conforms to the OpenAPI specification to be utilized by intelligent agents**. Through this framework, we can:

- **Unified Integration**: Bring diverse APIs into the Agent framework
- **Intelligent Interaction**: Enable AI models to understand API capabilities and limitations, automatically calling appropriate endpoints
- **Multi-Model Support**: Support OpenAI, local models, and third-party AI services
- **Process Orchestration**: Solve complex problems through multi-Agent collaboration

Our extensions make this framework no longer limited to OpenAI's cloud-based models, but support various local and third-party models, providing broader possibilities for API integration.



## Current Progress

We have implemented integration with **Ollama**, which is an important milestone:

- Use locally-running open-source models (such as Llama, Mistral, Phi, etc.)
- Communicate via Ollama's OpenAI-compatible API endpoints
- Experience the power of the Agent framework without requiring OpenAI API keys
- Ensure data privacy with all processing happening locally

### Examples passed with Ollama
examples_OpenAPI/ollama/basic/hello_world.py
examples_OpenAPI/ollama/basic/hello_world_jupyter.py
examples_OpenAPI/ollama/basic/hello_world.py
examples_OpenAPI/ollama/basic/failed/agent_lifecycle_example.py
examples_OpenAPI/ollama/basic/failed/lifecycle_example.py





## Core Concepts

1. [**Agents**](https://openai.github.io/openai-agents-python/agents): LLMs configured with instructions, tools, guardrails, and handoffs
2. [**Handoffs**](https://openai.github.io/openai-agents-python/handoffs/): A specialized tool call used by the Agents SDK for transferring control between agents
3. [**Guardrails**](https://openai.github.io/openai-agents-python/guardrails/): Configurable safety checks for input and output validation
4. [**Tracing**](https://openai.github.io/openai-agents-python/tracing/): Built-in tracking of agent runs, allowing you to view, debug and optimize your workflows
5. [**Model Providers**](https://openai.github.io/openai-agents-python/ollama_integration): Support for multiple model providers including OpenAI and Ollama

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
        ollama_default_model="llama3.2",  # Use phi4 model
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
3. Pull the required model, e.g., `ollama pull phi4:latest`

## Project Structure

- **`src/`**: Core source code
  - **`agents/`**: Main implementation of the Agent framework
  - **`agents/models/`**: Implementations of different model providers
- **`examples_OpenAPI/`**: OpenAPI integration examples
  - **`ollama/`**: Ollama integration examples
- **`docs/`**: Project documentation
  - **`ollama_integration.md`**: Detailed documentation on Ollama integration
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

## Acknowledgements

We would like to thank the open-source community for their outstanding work, especially:

- [OpenAI](https://openai.com/) (original Agents SDK)
- [Ollama](https://ollama.ai/) (local LLM runtime framework)
- [Pydantic](https://docs.pydantic.dev/latest/) (data validation) and [PydanticAI](https://ai.pydantic.dev/) (advanced agent framework)
- [MkDocs](https://github.com/squidfunk/mkdocs-material)
- [Griffe](https://github.com/mkdocstrings/griffe)

## Contributions

Contributions are welcome! You can participate in the following ways:

1. Add support for new APIs
2. Improve existing integrations
3. Enhance documentation
4. Report issues or suggest features

We are committed to building this SDK into a universal API and Agent integration framework, enabling any API to leverage the intelligent capabilities of Agent technology.
