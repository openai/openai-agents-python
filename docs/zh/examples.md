---
search:
  exclude: true
---
# 示例

请在 [repo](https://github.com/openai/openai-agents-python/tree/main/examples) 的示例部分查看 SDK 的多种 sample code。示例按多个目录组织，展示了不同的模式和能力。

## 目录

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    此目录中的示例展示了常见的智能体设计模式，例如

    -   确定性工作流
    -   Agents as tools
    -   带流式事件的 Agents as tools（`examples/agent_patterns/agents_as_tools_streaming.py`）
    -   带结构化输入参数的 Agents as tools（`examples/agent_patterns/agents_as_tools_structured.py`）
    -   并行智能体执行
    -   条件化工具使用
    -   通过不同行为强制工具使用（`examples/agent_patterns/forcing_tool_use.py`）
    -   输入/输出安全防护措施
    -   LLM 作为裁判
    -   路由
    -   流式安全防护措施
    -   带工具审批与状态序列化的人在回路（`examples/agent_patterns/human_in_the_loop.py`）
    -   带流式传输的人在回路（`examples/agent_patterns/human_in_the_loop_stream.py`）
    -   审批流程的自定义拒绝消息（`examples/agent_patterns/human_in_the_loop_custom_rejection.py`）

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    这些示例展示了 SDK 的基础能力，例如

    -   Hello World 示例（默认模型、GPT-5、开放权重模型）
    -   智能体生命周期管理
    -   Run hooks 和 agent hooks 生命周期示例（`examples/basic/lifecycle_example.py`）
    -   动态系统提示词
    -   基础工具使用（`examples/basic/tools.py`）
    -   工具输入/输出安全防护措施（`examples/basic/tool_guardrails.py`）
    -   图像工具输出（`examples/basic/image_tool_output.py`）
    -   流式输出（文本、条目、函数调用参数）
    -   跨轮次共享会话助手的 Responses websocket 传输（`examples/basic/stream_ws.py`）
    -   提示词模板
    -   文件处理（本地与远程、图像与 PDF）
    -   用量追踪
    -   由 Runner 管理的重试设置（`examples/basic/retry.py`）
    -   通过第三方适配器由 Runner 管理重试（`examples/basic/retry_litellm.py`）
    -   非严格输出类型
    -   previous response ID 用法

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空公司的客户服务系统示例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    一个金融研究智能体，展示了使用智能体和工具进行金融数据分析的结构化研究工作流。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    智能体任务转移的实用示例，包含消息过滤，包括：

    -   消息过滤示例（`examples/handoffs/message_filter.py`）
    -   带流式传输的消息过滤（`examples/handoffs/message_filter_streaming.py`）

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    展示如何将托管 MCP（Model context protocol）与 OpenAI Responses API 一起使用的示例，包括：

    -   无需审批的简单托管 MCP（`examples/hosted_mcp/simple.py`）
    -   MCP 连接器，例如 Google Calendar（`examples/hosted_mcp/connectors.py`）
    -   基于中断审批的人在回路（`examples/hosted_mcp/human_in_the_loop.py`）
    -   MCP 工具调用的审批回调（`examples/hosted_mcp/on_approval.py`）

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    了解如何使用 MCP（Model context protocol）构建智能体，包括：

    -   文件系统示例
    -   Git 示例
    -   MCP prompt 服务示例
    -   SSE（服务器发送事件）示例
    -   SSE 远程服务连接（`examples/mcp/sse_remote_example`）
    -   Streamable HTTP 示例
    -   Streamable HTTP 远程连接（`examples/mcp/streamable_http_remote_example`）
    -   用于 Streamable HTTP 的自定义 HTTP 客户端工厂（`examples/mcp/streamablehttp_custom_client_example`）
    -   使用 `MCPUtil.get_all_function_tools` 预获取所有 MCP 工具（`examples/mcp/get_all_mcp_tools_example`）
    -   搭配 FastAPI 的 MCPServerManager（`examples/mcp/manager_example`）
    -   MCP 工具过滤（`examples/mcp/tool_filter_example`）

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    面向智能体的不同内存实现示例，包括：

    -   SQLite 会话存储
    -   高级 SQLite 会话存储
    -   Redis 会话存储
    -   SQLAlchemy 会话存储
    -   Dapr 状态存储会话存储
    -   加密会话存储
    -   OpenAI Conversations 会话存储
    -   Responses 压缩会话存储
    -   使用 `ModelSettings(store=False)` 的无状态 Responses 压缩（`examples/memory/compaction_session_stateless_example.py`）
    -   文件后端会话存储（`examples/memory/file_session.py`）
    -   带人在回路的文件后端会话（`examples/memory/file_hitl_example.py`）
    -   带人在回路的 SQLite 内存会话（`examples/memory/memory_session_hitl_example.py`）
    -   带人在回路的 OpenAI Conversations 会话（`examples/memory/openai_session_hitl_example.py`）
    -   跨会话的 HITL 审批/拒绝场景（`examples/memory/hitl_session_scenario.py`）

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    探索如何在 SDK 中使用非 OpenAI 模型，包括自定义提供方和第三方适配器。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    展示如何使用 SDK 构建实时体验的示例，包括：

    -   使用结构化文本和图像消息的 Web 应用模式
    -   命令行音频循环与播放处理
    -   基于 WebSocket 的 Twilio Media Streams 集成
    -   使用 Realtime Calls API attach 流程的 Twilio SIP 集成

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    展示如何处理推理内容的示例，包括：

    -   使用 Runner API 的推理内容，含流式和非流式（`examples/reasoning_content/runner_example.py`）
    -   通过 OpenRouter 使用 OSS 模型的推理内容（`examples/reasoning_content/gpt_oss_stream.py`）
    -   基础推理内容示例（`examples/reasoning_content/main.py`）

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    简单的深度研究克隆示例，展示复杂的多智能体研究工作流。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    了解如何实现由OpenAI托管的工具和实验性 Codex 工具能力，例如：

    -   网络检索以及带过滤器的网络检索
    -   文件检索
    -   Code Interpreter
    -   带文件编辑与审批的 apply patch 工具（`examples/tools/apply_patch.py`）
    -   带审批回调的 shell 工具执行（`examples/tools/shell.py`）
    -   带基于中断审批的人在回路 shell 工具（`examples/tools/shell_human_in_the_loop.py`）
    -   带内联技能的托管容器 shell（`examples/tools/container_shell_inline_skill.py`）
    -   带技能引用的托管容器 shell（`examples/tools/container_shell_skill_reference.py`）
    -   带本地技能的本地 shell（`examples/tools/local_shell_skill.py`）
    -   带命名空间和延迟工具的工具搜索（`examples/tools/tool_search.py`）
    -   计算机操作
    -   图像生成
    -   实验性 Codex 工具工作流（`examples/tools/codex.py`）
    -   实验性 Codex 同线程工作流（`examples/tools/codex_same_thread.py`）

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    查看语音智能体示例，使用我们的 TTS 和 STT 模型，包括流式语音示例。