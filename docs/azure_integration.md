# Azure OpenAI Integration

This document provides detailed guidance on integrating Azure OpenAI Service with the OpenAPI Agents SDK.

## Overview

Azure OpenAI Service provides enterprise-grade security, compliance, and regional availability for OpenAI's powerful language models. This integration allows you to leverage Azure's robust platform while maintaining compatibility with the Agents SDK framework.

## Key Benefits

- **Enterprise-grade Security**: Benefit from Azure's comprehensive security features, including Private Link, Managed Identity, and more
- **Regional Availability**: Deploy and run models in regions that comply with your data residency requirements
- **Compliance**: Take advantage of Microsoft's compliance certifications and commitments
- **Seamless Integration**: Use the Agents SDK with minimal changes to your existing code

## Setup Requirements

Before starting, ensure you have:

1. An Azure subscription
2. Access to Azure OpenAI Service (requires approval)
3. One or more deployed model endpoints in your Azure OpenAI resource

## Configuration

### Step 1: Create Azure OpenAI Resources

1. Create an Azure OpenAI resource in the [Azure Portal](https://portal.azure.com)
2. Deploy models through the Azure OpenAI Studio
3. Note your API endpoint, key, and deployment names

### Step 2: Configure the SDK

```python
from src.agents import Agent, Runner
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

# Create Azure OpenAI model settings
azure_settings = ModelSettings(
    provider="azure_openai",
    azure_endpoint="https://your-resource-name.openai.azure.com",
    azure_api_key="your-azure-api-key",
    azure_api_version="2024-02-15-preview",  # Use appropriate API version
    azure_deployment="your-deployment-name",  # Maps to a model in Azure
    temperature=0.7
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
```

## Usage Examples

### Basic Chat Completion

```python
import asyncio

async def main():
    # Setup agent with Azure settings (as shown above)
    
    # Run the agent
    result = await Runner.run(
        agent,
        "Explain the benefits of using Azure OpenAI Service.",
        run_config=run_config
    )
    
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

### Using Tools with Azure OpenAI

Azure OpenAI supports tool calling with compatible models (e.g., GPT-4 and GPT-3.5-Turbo):

```python
import asyncio
from pydantic import BaseModel, Field
from typing import List

# Define a tool using Pydantic
class WeatherTool(BaseModel):
    """Get the current weather for a location"""
    location: str = Field(..., description="City and state, e.g., San Francisco, CA")
    
    def get_weather(self) -> str:
        # This would be a real API call in production
        return f"It's sunny and 72°F in {self.location}"

async def main():
    # Setup agent with Azure settings
    
    # Add tool to agent
    agent.add_tool(
        "get_weather",
        WeatherTool,
        WeatherTool.get_weather
    )
    
    # Run agent
    result = await Runner.run(
        agent,
        "What's the weather in Seattle?",
        run_config=run_config
    )
    
    print(result.final_output)
```

## Supported Features

Azure OpenAI integration supports the following features:

| Feature | Support Status | Notes |
|---------|----------------|-------|
| Chat Completions | ✅ Full | Compatible with all models |
| Tool Calling | ✅ Full | Compatible with newer models |
| Function Calling | ✅ Full | Legacy feature, use Tool Calling |
| JSON Mode | ✅ Full | Available with compatible models |
| Vision Analysis | ✅ Full | With GPT-4 Vision models |
| Stream Mode | ✅ Full | Works with all supported models |
| Multi-Agent Handoffs | ✅ Full | Fully supported |
| Response Format Control | ✅ Full | JSON/text output control |
| Responses API | ❌ Not Available | Not currently supported by Azure |

## Common Issues and Solutions

### Issue: Authentication Errors

**Solution**: Ensure your API key is correct and that your deployment is active. Check that your resource has sufficient quota.

### Issue: Model Not Found

**Solution**: Verify deployment name in your Azure settings. Ensure model is deployed in your Azure OpenAI Studio.

### Issue: Rate Limiting

**Solution**: Implement retry logic with exponential backoff. Consider requesting quota increases for production workloads.

### Issue: Tool Calling Not Working

**Solution**: Ensure you're using a model version that supports tool calling (e.g., GPT-4 or GPT-3.5-Turbo). Check that your tool definitions follow the required format.

## Advanced Configuration

### Regional Endpoints

Azure OpenAI is available in multiple regions. Configure your endpoint accordingly:

```python
azure_settings = ModelSettings(
    provider="azure_openai",
    azure_endpoint="https://your-resource-name.eastus.api.cognitive.microsoft.com",
    # Other settings as above
)
```

### Private Link Support

For enhanced security, configure Private Link:

```python
azure_settings = ModelSettings(
    provider="azure_openai",
    azure_endpoint="https://your-private-link-endpoint.com",
    # Other settings as above
)
```

### Managed Identity Authentication

Instead of API keys, use Azure Managed Identity:

```python
azure_settings = ModelSettings(
    provider="azure_openai",
    azure_endpoint="https://your-resource-name.openai.azure.com",
    azure_ad_token_provider=get_managed_identity_token,  # Function that returns token
    azure_api_version="2024-02-15-preview"
)
```

## Performance Optimizations

1. **Co-locate Resources**: Deploy your application in the same region as your Azure OpenAI resource
2. **Use Connection Pooling**: For high-volume applications
3. **Implement Caching**: For repeated or similar requests
4. **Right-size Context**: Minimize token usage by sending only necessary context

## Examples Repository

For complete working examples, see the `examples_OpenAPI/azure` directory in this repository.

## Additional Resources

- [Azure OpenAI Documentation](https://learn.microsoft.com/en-us/azure/ai-services/openai/)
- [Azure OpenAI Pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/)
- [Azure OpenAI Quotas and Limits](https://learn.microsoft.com/en-us/azure/ai-services/openai/quotas-limits)
- [Azure OpenAI REST API Reference](https://learn.microsoft.com/en-us/azure/ai-services/openai/reference)
- [Azure Security Best Practices](https://learn.microsoft.com/en-us/azure/security/fundamentals/best-practices-and-patterns)

## Comparing with OpenAI API

### Key Differences

| Feature | Azure OpenAI | OpenAI API |
|---------|-------------|------------|
| Authentication | API Key, Microsoft Entra ID | API Key only |
| Regional Deployment | Multiple regions available | Fixed endpoints |
| Rate Limiting | Per deployment, adjustable | Account-wide |
| Compliance | Enterprise compliance certifications | Consumer-focused |
| Cost Management | Azure subscription integrations | Direct billing |
| Network Security | Private Link, VNet integration | Public endpoints |
| Responses API | Not currently available | Available |

### When to Choose Azure OpenAI

- Enterprise applications requiring compliance certifications
- Applications with strict data residency requirements
- Systems needing enhanced security features
- Organizations already invested in Azure ecosystem
- Scenarios requiring fine-grained access control

## Multi-Provider Strategy

You can implement a multi-provider strategy to use both Azure OpenAI and other providers in your application:

```python
# Configure providers
azure_settings = ModelSettings(
    provider="azure_openai",
    azure_endpoint="https://your-resource-name.openai.azure.com",
    azure_api_key="your-azure-api-key",
    azure_api_version="2024-02-15-preview",
    azure_deployment="gpt-4"
)

openai_settings = ModelSettings(
    provider="openai",
    api_key="your-openai-api-key",
    model="gpt-4"
)

# Create agents for different purposes
azure_agent = Agent(
    name="EnterpriseAssistant",
    instructions="You handle enterprise inquiries securely.",
    model_settings=azure_settings
)

openai_agent = Agent(
    name="GeneralAssistant",
    instructions="You handle general public inquiries.",
    model_settings=openai_settings
)

# Use different agents based on your requirements
```

## Migration from OpenAI API

If you're migrating from the OpenAI API to Azure OpenAI, here are the key changes:

1. Update model settings to use `provider="azure_openai"`
2. Add Azure-specific configuration (endpoint, deployment name, etc.)
3. Update model references from model names (e.g., "gpt-4") to deployment names
4. Implement Azure authentication methods
5. Review and update rate limiting and retry strategies

Example migration:

```python
# Before: OpenAI API
openai_settings = ModelSettings(
    provider="openai",
    api_key="your-openai-api-key",
    model="gpt-4"
)

# After: Azure OpenAI
azure_settings = ModelSettings(
    provider="azure_openai",
    azure_endpoint="https://your-resource-name.openai.azure.com",
    azure_api_key="your-azure-api-key",
    azure_api_version="2024-02-15-preview",
    azure_deployment="gpt4-deployment"  # Name of your GPT-4 deployment in Azure
)
```

## Conclusion

Azure OpenAI Service offers a robust, enterprise-ready platform for deploying AI applications with the OpenAPI Agents SDK. This integration combines the power of OpenAI's models with Azure's enterprise capabilities, providing a secure and compliant solution for organizations of all sizes.

By following this guide, you should be able to successfully integrate your Agents SDK applications with Azure OpenAI Service, taking advantage of both the flexibility of the SDK and the enterprise features of Azure.
