---
search:
  exclude: true
---
# 代码示例

在[仓库](https://github.com/openai/openai-agents-python/tree/main/examples)的 examples 部分可以查看各种 SDK 的示例实现。这些代码示例按多个目录组织，以演示不同的模式与能力。

## 目录

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    本目录中的代码示例展示常见的智能体设计模式，例如：

    -   确定性工作流
    -   作为工具的智能体
    -   并行智能体执行
    -   条件性工具使用
    -   输入/输出安全防护措施
    -   LLM 充当裁判
    -   路由
    -   流式传输安全防护措施

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    这些代码示例展示 SDK 的基础能力，例如：

    -   Hello World 代码示例（默认模型、GPT-5、开源权重模型）
    -   智能体生命周期管理
    -   动态 system prompt
    -   流式传输输出（文本、items、函数调用参数）
    -   提示词模板
    -   文件处理（本地与远程、图像与 PDF）
    -   使用情况追踪
    -   非严格输出类型
    -   先前响应 ID 的用法

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空公司的客户服务系统示例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    一个金融研究智能体，展示使用智能体与工具的结构化研究工作流，用于金融数据分析。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    查看带消息过滤的智能体任务转移的实用代码示例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    演示如何使用托管的 MCP (Model Context Protocol) 连接器与审批。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    学习如何使用 MCP (Model Context Protocol) 构建智能体，包括：

    -   文件系统代码示例
    -   Git 代码示例
    -   MCP prompt 服务 代码示例
    -   SSE (Server-Sent Events) 代码示例
    -   可流式传输的 HTTP 代码示例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    展示智能体的不同内存实现，包括：

    -   SQLite 会话存储
    -   高级 SQLite 会话存储
    -   Redis 会话存储
    -   SQLAlchemy 会话存储
    -   加密会话存储
    -   OpenAI 会话存储

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    探索如何在 SDK 中使用非 OpenAI 模型，包括自定义提供方与 LiteLLM 集成。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    展示如何使用 SDK 构建实时体验，包括：

    -   Web 应用
    -   命令行界面
    -   与 Twilio 的集成

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    演示如何处理推理内容与格式良好的数据。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    简易的深度研究克隆，演示复杂的多智能体研究工作流。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    学习如何实现由OpenAI托管的工具，例如：

    -   网络检索与带筛选的网络检索
    -   文件检索
    -   Code Interpreter
    -   计算机操作
    -   图像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    查看语音智能体代码示例，使用我们的 TTS 和 STT 模型，包括流式传输语音代码示例。