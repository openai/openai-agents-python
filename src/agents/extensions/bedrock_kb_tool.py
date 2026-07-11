"""Amazon Bedrock Knowledge Base tool for OpenAI Agents SDK.

Provides a retrieval tool that queries Amazon Bedrock Managed Knowledge Bases.

Usage:
    from agents import Agent
    from agents.extensions.bedrock_kb_tool import create_bedrock_kb_tool

    kb_tool = create_bedrock_kb_tool(knowledge_base_id="ABCDEFGHIJ")
    agent = Agent(name="assistant", tools=[kb_tool])
"""

import os
from typing import Optional

from agents import function_tool


def _get_source_uri(result: dict) -> str:
    """Extract source URI from a retrieval result, handling all location types."""
    location = result.get('location', {})
    loc_type = location.get('type', '')
    if loc_type == 'S3' or 's3Location' in location:
        return location.get('s3Location', {}).get('uri', '')
    elif loc_type == 'WEB' or 'webLocation' in location:
        return location.get('webLocation', {}).get('url', '')
    elif 'confluenceLocation' in location:
        return location.get('confluenceLocation', {}).get('url', '')
    elif 'salesforceLocation' in location:
        return location.get('salesforceLocation', {}).get('url', '')
    elif 'sharePointLocation' in location:
        return location.get('sharePointLocation', {}).get('url', '')
    elif 'customDocumentLocation' in location:
        return location.get('customDocumentLocation', {}).get('id', '')
    # Fallback to metadata._source_uri (for agentic results)
    return result.get('metadata', {}).get('_source_uri', '')


def create_bedrock_kb_tool(
    knowledge_base_id: Optional[str] = None,
    region_name: Optional[str] = None,
    number_of_results: int = 5,
    knowledge_base_type: str = "MANAGED",
    use_agentic_retrieval: Optional[bool] = None,
):
    """Create a Bedrock Knowledge Base retrieval tool.

    Args:
        knowledge_base_id: The KB ID. Falls back to KNOWLEDGE_BASE_ID env var.
        region_name: AWS region. Falls back to AWS_REGION env var or us-east-1.
        number_of_results: Max results to return.
        knowledge_base_type: "MANAGED" (recommended) or "VECTOR".
        use_agentic_retrieval: Use AgenticRetrieveStream for complex queries with
            query decomposition and managed reranking. Falls back to plain Retrieve
            on failure. Defaults to True.

    Returns:
        A function tool that can be passed to an Agent's tools list.
    """
    kb_id = knowledge_base_id or os.environ.get("KNOWLEDGE_BASE_ID", "")
    region = region_name or os.environ.get("AWS_REGION", "us-east-1")
    _use_agentic = use_agentic_retrieval if use_agentic_retrieval is not None else os.environ.get('USE_AGENTIC_RETRIEVAL', 'true').lower() != 'false'
    _client = None

    def _get_client():
        nonlocal _client
        if _client is None:
            try:
                import boto3
            except ImportError:
                raise ImportError(
                    "boto3 is required for Bedrock KB tool. "
                    "Install with: pip install boto3>=1.41.0"
                )
            _client = boto3.client("bedrock-agent-runtime", region_name=region)
        return _client

    def _managed_retrieve(query: str) -> str:
        """Retrieve using plain managed Retrieve API."""
        client = _get_client()

        if knowledge_base_type == "MANAGED":
            retrieval_config = {
                "managedSearchConfiguration": {
                    "numberOfResults": number_of_results
                }
            }
        else:
            retrieval_config = {
                "vectorSearchConfiguration": {
                    "numberOfResults": number_of_results
                }
            }

        response = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration=retrieval_config,
        )

        results = response.get("retrievalResults", [])
        if not results:
            return "No relevant documents found in the knowledge base."

        passages = []
        for i, result in enumerate(results, 1):
            content = result.get("content", {}).get("text", "")
            source = _get_source_uri(result) or "unknown"
            passages.append(f"[{i}] {content}\nSource: {source}")

        return "\n\n".join(passages)

    def _agentic_retrieve(query: str) -> str:
        """Retrieve using AgenticRetrieveStream with fallback to plain Retrieve."""
        try:
            client = _get_client()
            response = client.agentic_retrieve_stream(
                knowledgeBaseId=kb_id,
                messages=[{"content": {"text": query}, "role": "user"}],
                retrievers=[{
                    "configuration": {
                        "knowledgeBase": {
                            "knowledgeBaseId": kb_id,
                            "retrievalOverrides": {
                                "maxNumberOfResults": number_of_results
                            },
                        }
                    }
                }],
                agenticRetrieveConfiguration={
                    "foundationModelType": "MANAGED",
                    "rerankingModelType": "MANAGED",
                },
            )
            # Process streaming response
            passages = []
            for event in response.get("stream", []):
                if "result" in event and "results" in event["result"]:
                    for result in event["result"]["results"]:
                        content = result.get("content", {}).get("text", "")
                        source = _get_source_uri(result) or "unknown"
                        passages.append(f"{content}\nSource: {source}")
            if not passages:
                return "No relevant documents found in the knowledge base."
            return "\n\n".join(passages)
        except Exception:
            # Fall back to plain managed retrieve
            return _managed_retrieve(query)

    @function_tool
    def bedrock_knowledge_base(query: str) -> str:
        """Retrieve relevant documents from an Amazon Bedrock Knowledge Base.

        Use this tool to search a knowledge base for information relevant to the user's question.

        Args:
            query: The search query to find relevant documents.
        """
        if _use_agentic:
            return _agentic_retrieve(query)
        return _managed_retrieve(query)

    # Disable strict JSON schema for compatibility with non-OpenAI providers (e.g., Bedrock via LiteLLM)
    bedrock_knowledge_base.strict_json_schema = False

    return bedrock_knowledge_base
