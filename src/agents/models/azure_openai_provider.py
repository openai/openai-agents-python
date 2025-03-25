from __future__ import annotations

import os
import httpx
from openai import AsyncAzureOpenAI, DefaultAsyncHttpxClient

from . import _openai_shared
from .interface import Model, ModelProvider
from .openai_chatcompletions import OpenAIChatCompletionsModel
from .openai_responses import OpenAIResponsesModel

DEFAULT_API_VERSION = "2025-01-01-preview"  # 更改为Azure OpenAI更广泛支持的API版本
DEFAULT_DEPLOYMENT = "gpt-4o"  # 默认部署名称

_http_client: httpx.AsyncClient | None = None


# 与 OpenAI Provider 类似，共享 HTTP 客户端以提高性能
def shared_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = DefaultAsyncHttpxClient()
    return _http_client


class AzureOpenAIProvider(ModelProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        azure_endpoint: str | None = None,
        api_version: str | None = None,
        base_url: str | None = None,
        deployment: str | None = None,
        openai_client: AsyncAzureOpenAI | None = None,
        use_responses: bool | None = None,
    ) -> None:
        """创建新的 Azure OpenAI 提供程序。

        Args:
            api_key: 用于 Azure OpenAI 客户端的 API 密钥。如果未提供，将从环境变量获取。
            azure_endpoint: Azure OpenAI 端点，例如 "https://{resource-name}.openai.azure.com"。如果未提供，将从环境变量获取。
            api_version: Azure OpenAI API 版本。默认为 "2025-01-01-preview"。
            base_url: 可选的完整基础 URL。如果提供，将覆盖 azure_endpoint。如果未提供，将从环境变量获取。
            deployment: Azure 部署名称。默认为 "gpt-4o"。
            openai_client: 可选的 Azure OpenAI 客户端实例。如提供，将忽略其他客户端参数。
            use_responses: 是否使用 OpenAI Responses API。注意：Azure OpenAI可能不支持标准的Responses API路径。
        """
        if openai_client is not None:
            assert api_key is None and azure_endpoint is None and base_url is None, (
                "提供 openai_client 时不要再提供 api_key、azure_endpoint 或 base_url"
            )
            self._client: AsyncAzureOpenAI | None = openai_client
        else:
            self._client = None
            # 自动从环境变量获取参数，如果参数未提供
            self._stored_api_key = api_key or os.getenv("AZURE_OPENAI_API_KEY")
            self._stored_azure_endpoint = azure_endpoint or os.getenv("AZURE_OPENAI_ENDPOINT")
            self._stored_base_url = base_url or os.getenv("AZURE_OPENAI_BASE_URL")
            self._stored_api_version = api_version or os.getenv("AZURE_OPENAI_API_VERSION") or DEFAULT_API_VERSION
            self._stored_deployment = deployment or os.getenv("AZURE_OPENAI_DEPLOYMENT") or DEFAULT_DEPLOYMENT

        # 默认不使用Responses API，因为Azure OpenAI的API路径与标准OpenAI不同
        self._use_responses = False if use_responses is None else use_responses

    # 延迟加载客户端，确保只有在实际使用时才创建客户端实例
    def _get_client(self) -> AsyncAzureOpenAI:
        if self._client is None:
            if not self._stored_api_key:
                raise ValueError("Azure OpenAI API 密钥未提供，请设置 AZURE_OPENAI_API_KEY 环境变量或在构造函数中提供")
            
            # 确定基础 URL
            base_url = self._stored_base_url or self._stored_azure_endpoint
            if not base_url:
                raise ValueError("Azure OpenAI 端点未提供，请设置 AZURE_OPENAI_ENDPOINT 或 AZURE_OPENAI_BASE_URL 环境变量，或在构造函数中提供")

            self._client = AsyncAzureOpenAI(
                api_key=self._stored_api_key,
                api_version=self._stored_api_version,
                azure_endpoint=base_url,
                http_client=shared_http_client(),
            )

        return self._client

    def get_model(self, model_name: str | None) -> Model:
        """获取指定名称的模型实例
        
        Args:
            model_name: 模型名称，在 Azure OpenAI 中通常是部署名称
            
        Returns:
            Model: 模型实例
        """
        # 在 Azure OpenAI 中，model_name 实际上是部署名称
        deployment_name = model_name if model_name else self._stored_deployment
        
        client = self._get_client()

        # 由于Azure OpenAI的URL格式要求，除非明确指定，否则使用ChatCompletions API
        return (
            OpenAIResponsesModel(model=deployment_name, openai_client=client)
            if self._use_responses
            else OpenAIChatCompletionsModel(model=deployment_name, openai_client=client)
        )
    
    @staticmethod
    def from_env() -> AzureOpenAIProvider:
        """从环境变量创建 AzureOpenAIProvider 实例
        
        环境变量:
            AZURE_OPENAI_API_KEY: Azure OpenAI API 密钥
            AZURE_OPENAI_ENDPOINT: Azure OpenAI 端点
            AZURE_OPENAI_BASE_URL: (可选) 替代完整基础URL (覆盖 AZURE_OPENAI_ENDPOINT)
            AZURE_OPENAI_API_VERSION: (可选) API 版本
            AZURE_OPENAI_DEPLOYMENT: (可选) 部署名称
        
        Returns:
            AzureOpenAIProvider: 配置好的实例
        """
        return AzureOpenAIProvider(
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            base_url=os.getenv("AZURE_OPENAI_BASE_URL"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
            deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT"),
        )
