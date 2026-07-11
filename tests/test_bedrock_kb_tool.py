"""Tests for agents.extensions.bedrock_kb_tool.create_bedrock_kb_tool factory."""

from unittest.mock import MagicMock, patch

import pytest

from agents.extensions.bedrock_kb_tool import create_bedrock_kb_tool
from agents.tool import FunctionTool


class TestFactoryReturnsFunctionTool:
    def test_returns_function_tool_instance(self) -> None:
        tool = create_bedrock_kb_tool(knowledge_base_id="KB123")
        assert isinstance(tool, FunctionTool)

    def test_tool_has_expected_name(self) -> None:
        tool = create_bedrock_kb_tool(knowledge_base_id="KB123")
        assert tool.name == "bedrock_knowledge_base"

    def test_tool_has_description(self) -> None:
        tool = create_bedrock_kb_tool(knowledge_base_id="KB123")
        assert tool.description
        assert "knowledge base" in tool.description.lower()


class TestManagedSearchConfiguration:
    @pytest.mark.asyncio
    async def test_retrieve_uses_managed_search_by_default(self) -> None:
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": "Some relevant text"},
                    "location": {"s3Location": {"uri": "s3://bucket/doc.pdf"}},
                }
            ]
        }

        with patch("boto3.client", return_value=mock_client):
            tool = create_bedrock_kb_tool(
                knowledge_base_id="KB123",
                region_name="us-west-2",
                number_of_results=3,
            )
            result = await tool.on_invoke_tool(MagicMock(), '{"query": "test query"}')

        mock_client.retrieve.assert_called_once_with(
            knowledgeBaseId="KB123",
            retrievalQuery={"text": "test query"},
            retrievalConfiguration={
                "managedSearchConfiguration": {"numberOfResults": 3}
            },
        )
        assert "Some relevant text" in result
        assert "s3://bucket/doc.pdf" in result

    @pytest.mark.asyncio
    async def test_retrieve_uses_managed_search_when_type_is_managed(self) -> None:
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": "Result text"},
                    "location": {"s3Location": {"uri": "s3://b/key"}},
                }
            ]
        }

        with patch("boto3.client", return_value=mock_client):
            tool = create_bedrock_kb_tool(
                knowledge_base_id="KB456",
                knowledge_base_type="MANAGED",
            )
            await tool.on_invoke_tool(MagicMock(), '{"query": "hello"}')

        call_kwargs = mock_client.retrieve.call_args[1]
        assert "managedSearchConfiguration" in call_kwargs["retrievalConfiguration"]


class TestVectorSearchConfiguration:
    @pytest.mark.asyncio
    async def test_retrieve_uses_vector_search_when_type_is_vector(self) -> None:
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": "Vector result"},
                    "location": {"s3Location": {"uri": "s3://bucket/file.txt"}},
                }
            ]
        }

        with patch("boto3.client", return_value=mock_client):
            tool = create_bedrock_kb_tool(
                knowledge_base_id="KB789",
                number_of_results=10,
                knowledge_base_type="VECTOR",
            )
            await tool.on_invoke_tool(MagicMock(), '{"query": "search term"}')

        mock_client.retrieve.assert_called_once_with(
            knowledgeBaseId="KB789",
            retrievalQuery={"text": "search term"},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": 10}
            },
        )

    @pytest.mark.asyncio
    async def test_vector_search_does_not_include_managed_config(self) -> None:
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {"retrievalResults": []}

        with patch("boto3.client", return_value=mock_client):
            tool = create_bedrock_kb_tool(
                knowledge_base_id="KB000",
                knowledge_base_type="VECTOR",
            )
            await tool.on_invoke_tool(MagicMock(), '{"query": "q"}')

        call_kwargs = mock_client.retrieve.call_args[1]
        assert "managedSearchConfiguration" not in call_kwargs["retrievalConfiguration"]
        assert "vectorSearchConfiguration" in call_kwargs["retrievalConfiguration"]


class TestEmptyResults:
    @pytest.mark.asyncio
    async def test_empty_results_returns_no_documents_message(self) -> None:
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {"retrievalResults": []}

        with patch("boto3.client", return_value=mock_client):
            tool = create_bedrock_kb_tool(knowledge_base_id="KB_EMPTY")
            result = await tool.on_invoke_tool(MagicMock(), '{"query": "nothing"}')

        assert result == "No relevant documents found in the knowledge base."

    @pytest.mark.asyncio
    async def test_missing_retrieval_results_key_returns_no_documents_message(self) -> None:
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {}

        with patch("boto3.client", return_value=mock_client):
            tool = create_bedrock_kb_tool(knowledge_base_id="KB_MISSING")
            result = await tool.on_invoke_tool(MagicMock(), '{"query": "missing"}')

        assert result == "No relevant documents found in the knowledge base."


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_boto3_import_error_returns_error_message(self) -> None:
        """When boto3 is not installed, the tool returns an error string to the model."""
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "boto3":
                raise ImportError("No module named 'boto3'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            tool = create_bedrock_kb_tool(knowledge_base_id="KB_ERR")
            result = await tool.on_invoke_tool(MagicMock(), '{"query": "fail"}')

        assert "boto3 is required" in result

    @pytest.mark.asyncio
    async def test_client_error_returns_error_message(self) -> None:
        """When the boto3 client raises, the tool returns an error string to the model."""
        mock_client = MagicMock()
        mock_client.retrieve.side_effect = Exception("Service unavailable")

        with patch("boto3.client", return_value=mock_client):
            tool = create_bedrock_kb_tool(knowledge_base_id="KB_FAIL")
            result = await tool.on_invoke_tool(MagicMock(), '{"query": "crash"}')

        assert "Service unavailable" in result


class TestEnvironmentVariableFallbacks:
    @pytest.mark.asyncio
    async def test_knowledge_base_id_from_env(self) -> None:
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {"retrievalResults": []}

        with (
            patch.dict("os.environ", {"KNOWLEDGE_BASE_ID": "ENV_KB_ID"}),
            patch("boto3.client", return_value=mock_client),
        ):
            tool = create_bedrock_kb_tool()
            await tool.on_invoke_tool(MagicMock(), '{"query": "env test"}')

        mock_client.retrieve.assert_called_once()
        call_kwargs = mock_client.retrieve.call_args[1]
        assert call_kwargs["knowledgeBaseId"] == "ENV_KB_ID"

    @pytest.mark.asyncio
    async def test_region_from_env(self) -> None:
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {"retrievalResults": []}

        with (
            patch.dict("os.environ", {"AWS_REGION": "eu-west-1"}),
            patch("boto3.client", return_value=mock_client) as mock_boto3_client,
        ):
            tool = create_bedrock_kb_tool(knowledge_base_id="KB_REGION")
            await tool.on_invoke_tool(MagicMock(), '{"query": "region test"}')

        mock_boto3_client.assert_called_once_with(
            "bedrock-agent-runtime", region_name="eu-west-1"
        )

    @pytest.mark.asyncio
    async def test_region_defaults_to_us_east_1(self) -> None:
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {"retrievalResults": []}

        env = {"AWS_REGION": ""}
        with (
            patch.dict("os.environ", env, clear=False),
            patch("boto3.client", return_value=mock_client) as mock_boto3_client,
        ):
            # Remove AWS_REGION to test default.
            with patch.dict("os.environ", {}, clear=True):
                tool = create_bedrock_kb_tool(knowledge_base_id="KB_DEFAULT")
            # Invoke triggers client creation.
            with patch("boto3.client", return_value=mock_client) as mock_boto3_client:
                await tool.on_invoke_tool(MagicMock(), '{"query": "default"}')

        mock_boto3_client.assert_called_once_with(
            "bedrock-agent-runtime", region_name="us-east-1"
        )

    @pytest.mark.asyncio
    async def test_explicit_params_override_env_vars(self) -> None:
        mock_client = MagicMock()
        mock_client.retrieve.return_value = {"retrievalResults": []}

        with (
            patch.dict(
                "os.environ",
                {"KNOWLEDGE_BASE_ID": "ENV_ID", "AWS_REGION": "ap-south-1"},
            ),
            patch("boto3.client", return_value=mock_client) as mock_boto3_client,
        ):
            tool = create_bedrock_kb_tool(
                knowledge_base_id="EXPLICIT_ID", region_name="us-west-2"
            )
            await tool.on_invoke_tool(MagicMock(), '{"query": "override"}')

        mock_boto3_client.assert_called_once_with(
            "bedrock-agent-runtime", region_name="us-west-2"
        )
        call_kwargs = mock_client.retrieve.call_args[1]
        assert call_kwargs["knowledgeBaseId"] == "EXPLICIT_ID"
