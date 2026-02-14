---
search:
  exclude: true
---
# 代码示例

请查看 [repo](https://github.com/openai/openai-agents-python/tree/main/examples) 的 examples 部分，其中提供了多种 SDK 的示例实现。这些代码示例按多个目录组织，用于展示不同的模式与能力。

## 目录

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    该目录中的代码示例展示了常见的智能体设计模式，例如：

    -   确定性工作流
    -   Agents as tools
    -   并行智能体执行
    -   条件式工具使用
    -   输入/输出安全防护措施
    -   LLM 作为评审
    -   路由
    -   流式传输安全防护措施

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    这些代码示例展示了 SDK 的基础能力，例如：

    -   Hello World 代码示例（默认模型、GPT-5、开源权重模型）
    -   智能体生命周期管理
    -   动态系统提示词
    -   流式传输输出（文本、条目、function call args）
    -   提示词模板
    -   文件处理（本地与远程、图像与 PDF）
    -   用量追踪
    -   非严格输出类型
    -   之前的响应 ID 用法

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    面向航空公司的客户服务系统示例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    一个金融研究智能体，演示了使用智能体与工具进行金融数据分析的结构化研究工作流。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    查看带消息过滤的智能体任务转移的实践示例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    演示如何使用托管的 MCP（Model Context Protocol）连接器与审批的代码示例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    学习如何使用 MCP（Model Context Protocol）构建智能体，包括：

    -   文件系统代码示例
    -   Git 代码示例
    -   MCP prompt server 代码示例
    -   SSE（Server-Sent Events）代码示例
    -   可流式 HTTP 代码示例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    不同的智能体记忆实现代码示例，包括：

    -   SQLite 会话存储
    -   高级 SQLite 会话存储
    -   Redis 会话存储
    -   SQLAlchemy 会话存储
    -   加密会话存储
    -   OpenAI 会话存储

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    了解如何将非 OpenAI 模型与 SDK 一起使用，包括自定义 provider 与 LiteLLM 集成。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    展示如何使用 SDK 构建实时体验的代码示例，包括：

    -   Web 应用
    -   命令行界面
    -   Twilio 集成
    -   Twilio SIP 集成

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    演示如何处理推理内容与 structured outputs 的代码示例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    简单的深度研究克隆示例，演示了复杂的多智能体研究工作流。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    学习如何实现由OpenAI托管的工具，以及实验性的 Codex 工具能力，例如：

    -   网络检索与带筛选条件的网络检索
    -   文件检索
    -   Code Interpreter
    -   带内联技能的托管容器 shell（`examples/tools/container_shell_inline_skill.py`）
    -   带技能引用的托管容器 shell（`examples/tools/container_shell_skill_reference.py`）
    -   计算机操作
    -   图像生成
    -   实验性 Codex 工具工作流（`examples/tools/codex.py`）
    -   实验性 Codex 同线程工作流（`examples/tools/codex_same_thread.py`）

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    查看语音智能体代码示例，使用我们的 TTS 与 STT 模型，包括流式语音代码示例。