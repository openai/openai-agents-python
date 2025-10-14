---
search:
  exclude: true
---
# 使用示例

在[仓库](https://github.com/openai/openai-agents-python/tree/main/examples)的示例部分查看SDK的各种示例实现。这些示例被组织成几个类别，展示了不同的模式和功能。

## 示例分类

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    此分类中的示例展示了常见的智能体设计模式，例如：

    -   确定性工作流
    -   智能体作为工具
    -   智能体并行执行
    -   条件工具使用
    -   输入/输出护栏
    -   LLM作为评判者
    -   路由
    -   流式护栏

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    这些示例展示了SDK的基础功能，例如：

    -   Hello World示例（默认模型、GPT-5、开源权重模型）
    -   智能体生命周期管理
    -   动态系统提示
    -   流式输出（文本、项目、函数调用参数）
    -   提示模板
    -   文件处理（本地和远程、图像和PDF）
    -   使用情况跟踪
    -   非严格输出类型
    -   先前响应ID使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空公司客户服务系统示例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融研究智能体，展示了使用智能体和工具进行金融数据分析的结构化研究工作流。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    查看带有消息过滤的智能体交接的实际示例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    展示如何使用托管MCP（模型上下文协议）连接器和批准的示例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    学习如何使用MCP（模型上下文协议）构建智能体，包括：

    -   文件系统示例
    -   Git示例
    -   MCP提示服务器示例
    -   SSE（服务器发送事件）示例
    -   可流式HTTP示例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    智能体的不同内存实现示例，包括：

    -   SQLite会话存储
    -   高级SQLite会话存储
    -   Redis会话存储
    -   SQLAlchemy会话存储
    -   加密会话存储
    -   OpenAI会话存储

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    探索如何在SDK中使用非OpenAI模型，包括自定义提供程序和LiteLLM集成。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    展示如何使用SDK构建实时体验的示例，包括：

    -   Web应用程序
    -   命令行界面
    -   Twilio集成

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    展示如何处理推理内容和结构化输出的示例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    简单的深度研究克隆，展示了复杂的多智能体研究工作流。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    学习如何实现OpenAI托管工具：

    -   网络搜索和带过滤的网络搜索
    -   文件搜索
    -   代码解释器
    -   计算机使用
    -   图像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    查看使用我们的TTS和STT模型的语音智能体示例，包括流式语音示例。