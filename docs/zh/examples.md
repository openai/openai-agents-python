---
search:
  exclude: true
---
# 代码示例

欢迎在 [repo](https://github.com/openai/openai-agents-python/tree/main/examples) 的代码示例部分查看 SDK 的多种示例实现。这些代码示例按多个目录组织，用于展示不同的模式与能力。

## 目录

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    此目录下的代码示例展示了常见的智能体设计模式，例如：

    -   确定性工作流
    -   Agents as tools
    -   并行智能体执行
    -   条件化工具使用
    -   输入/输出安全防护措施
    -   LLM 作为裁判
    -   路由
    -   流式传输安全防护措施

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    这些代码示例展示 SDK 的基础能力，例如：

    -   Hello world 代码示例（默认模型、GPT-5、开源权重模型）
    -   智能体生命周期管理
    -   动态系统提示词
    -   流式传输输出（文本、条目、函数调用参数）
    -   使用共享的跨轮次会话辅助工具进行 Responses websocket 传输（`examples/basic/stream_ws.py`）
    -   提示词模板
    -   文件处理（本地与远程、图片与 PDF）
    -   用量追踪
    -   非严格输出类型
    -   之前的 response ID 用法

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    面向航空公司的示例客服系统。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融研究智能体，展示使用智能体与工具的结构化研究工作流，用于金融数据分析。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    查看结合消息过滤的智能体任务转移实践代码示例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    展示如何使用托管的 MCP（Model Context Protocol）连接器与审批的代码示例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    学习如何使用 MCP（Model Context Protocol）构建智能体，包括：

    -   文件系统代码示例
    -   Git 代码示例
    -   MCP 提示词服务器代码示例
    -   SSE（Server-Sent Events）代码示例
    -   可流式 HTTP 代码示例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    不同智能体记忆实现的代码示例，包括：

    -   SQLite 会话存储
    -   高级 SQLite 会话存储
    -   Redis 会话存储
    -   SQLAlchemy 会话存储
    -   加密会话存储
    -   OpenAI 会话存储

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    探索如何在 SDK 中使用非 OpenAI 模型，包括自定义 provider 与 LiteLLM 集成。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    展示如何使用 SDK 构建实时体验的代码示例，包括：

    -   Web 应用
    -   命令行界面
    -   Twilio 集成
    -   Twilio SIP 集成

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    展示如何使用推理内容与 structured outputs 的代码示例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    简易深度研究克隆项目，展示复杂的多智能体研究工作流。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    学习如何实现 由OpenAI托管的工具 与实验性的 Codex 工具能力，例如：

    -   网络检索，以及带过滤器的网络检索
    -   文件检索
    -   Code Interpreter
    -   带内联技能的托管容器 shell（`examples/tools/container_shell_inline_skill.py`）
    -   带技能引用的托管容器 shell（`examples/tools/container_shell_skill_reference.py`）
    -   计算机操作
    -   图像生成
    -   实验性 Codex 工具工作流（`examples/tools/codex.py`）
    -   实验性 Codex 同线程工作流（`examples/tools/codex_same_thread.py`）

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    查看语音智能体的代码示例，使用我们的 TTS 与 STT 模型，包括流式语音代码示例。