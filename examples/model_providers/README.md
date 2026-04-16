# Local Model Providers Examples

This directory contains examples of using local/open-source models with the OpenAI Agents SDK.

## Gemma Local Provider

Use Google's Gemma models locally for completely offline, privacy-preserving agent execution.

### Setup

1. **Get HuggingFace Token**
   ```bash
   # Get token from https://huggingface.co/settings/tokens
   # Accept Gemma license at https://huggingface.co/google/gemma-2b-it
   ```

2. **Set Environment Variable**
   ```bash
   export HF_TOKEN=your_token_here
   ```

3. **Install Dependencies**
   ```bash
   pip install transformers torch accelerate bitsandbytes
   ```

### Usage

```python
from agents import Agent, Runner
from gemma_local_provider import create_gemma_provider

# Create local Gemma provider
provider = create_gemma_provider(model_name="google/gemma-2b-it")

# Create agent with local model
agent = Agent(
    name="LocalAssistant",
    instructions="You are a helpful assistant.",
    model_provider=provider,
)

# Run agent (completely offline!)
result = await Runner.run(agent, "Hello!")
print(result.final_output)
```

### Running the Example

```bash
python gemma_example.py
```

### Hardware Requirements

| Model | VRAM Required | Quantization | Speed (RTX 3060) |
|-------|--------------|--------------|------------------|
| gemma-2b-it | ~4GB | 4-bit | ~15s/response |
| gemma-2b-it | ~6GB | None | ~10s/response |

### Features

- ✅ GPU acceleration with CUDA
- ✅ 4-bit quantization for memory efficiency
- ✅ CPU fallback support
- ✅ Compatible with all Agents SDK features
- ✅ Streaming support

### Use Cases

- **Privacy-sensitive applications**: Healthcare, finance, legal
- **Offline environments**: Air-gapped systems, remote locations
- **Cost reduction**: No API fees for high-volume usage
- **Custom fine-tuning**: Use your own fine-tuned Gemma models

### Limitations

- Gemma 2B is smaller than GPT-4, so responses may be less sophisticated
- First load takes time to download model (~5GB)
- Slower than cloud APIs (10-20s vs 1-2s)

## Other Local Providers

Contributions welcome for:
- Llama 2/3 local provider
- Mistral local provider
- Custom fine-tuned model providers
