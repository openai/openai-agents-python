# Bedrock Managed Knowledge Base Support

## Overview
Adds an OpenAI Agents SDK tool extension that queries Amazon Bedrock Knowledge Bases for managed retrieval.

## Usage
```python
from agents import Agent, Runner
from agents.extensions.bedrock_kb import BedrockKnowledgeBaseTool

kb_tool = BedrockKnowledgeBaseTool(knowledge_base_id="YOUR_KB_ID")
agent = Agent(name="Assistant", tools=[kb_tool])
result = Runner.run_sync(agent, "What does our runbook say about incident response?")
print(result.final_output)
```

## Configuration
| Variable | Description | Default |
|---|---|---|
| KNOWLEDGE_BASE_ID | Bedrock Knowledge Base ID | None |
| AWS_REGION | AWS region for the KB | us-east-1 |
| AWS_PROFILE | AWS credentials profile | None |
| USE_AGENTIC_RETRIEVAL | Enable agentic retrieval | true |
| MAX_RESULTS | Maximum retrieval results | 5 |

## Features
- Managed search (no vector store needed)
- Agentic retrieval with query decomposition + reranking
- Automatic fallback to plain Retrieve if agentic fails
- Multi-source support (S3, Web, Confluence, SharePoint)
- Compatible with OpenAI Agents SDK FunctionTool interface

## SDK Requirements
- boto3 >= 1.43
- openai-agents >= 0.1

## Required IAM Permissions
```json
{
  "Effect": "Allow",
  "Action": [
    "bedrock:Retrieve",
    "bedrock:AgenticRetrieve"
  ],
  "Resource": "arn:aws:bedrock:<region>:<account-id>:knowledge-base/<kb-id>"
}
```

## References
- [Build a Managed Knowledge Base](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-build-managed.html)
- [Retrieve API](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-retrieve.html)
- [Agentic Retrieval](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-agentic.html)
