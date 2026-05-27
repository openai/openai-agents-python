---
search:
  exclude: true
---
# 示例

请查看[代码库](https://github.com/openai/openai-agents-python/tree/main/examples)的 examples 部分中 SDK 的各种示例实现。这些示例被组织为若干目录，展示不同的模式和能力。

## 目录

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    此目录中的示例展示常见的智能体设计模式，例如

    -   确定性工作流
    -   Agents as tools
    -   包含流式传输事件的Agents as tools（`examples/agent_patterns/agents_as_tools_streaming.py`）
    -   使用结构化输入参数的Agents as tools（`examples/agent_patterns/agents_as_tools_structured.py`）
    -   并行智能体执行
    -   条件式工具使用
    -   以不同行为强制使用工具（`examples/agent_patterns/forcing_tool_use.py`）
    -   输入/输出安全防护措施
    -   LLM作为评审者
    -   路由
    -   流式传输安全防护措施
    -   具备工具审批和状态序列化的人在回路（`examples/agent_patterns/human_in_the_loop.py`）
    -   具备流式传输的人在回路（`examples/agent_patterns/human_in_the_loop_stream.py`）
    -   审批流程的自定义拒绝消息（`examples/agent_patterns/human_in_the_loop_custom_rejection.py`）

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    这些示例展示 SDK 的基础能力，例如

    -   Hello world示例（默认模型、GPT-5、开放权重模型）
    -   智能体生命周期管理
    -   运行钩子和智能体钩子的生命周期示例（`examples/basic/lifecycle_example.py`）
    -   动态系统提示词
    -   基本工具使用（`examples/basic/tools.py`）
    -   工具输入/输出安全防护措施（`examples/basic/tool_guardrails.py`）
    -   图像工具输出（`examples/basic/image_tool_output.py`）
    -   流式传输输出（文本、项、函数调用参数）
    -   Responses WebSocket 传输，以及跨轮次共享的会话辅助工具（`examples/basic/stream_ws.py`）
    -   提示词模板
    -   文件处理（本地和远程、图像和 PDF）
    -   用量跟踪
    -   Runner 管理的重试设置（`examples/basic/retry.py`）
    -   通过第三方适配器由 Runner 管理的重试（`examples/basic/retry_litellm.py`）
    -   非严格输出类型
    -   先前响应 ID 的用法

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空公司的客户服务系统示例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    一个金融研究智能体，展示如何结合智能体和工具，为金融数据分析构建结构化研究工作流。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    智能体任务转移的实践示例，包含消息过滤，包括：

    -   消息过滤器示例（`examples/handoffs/message_filter.py`）
    -   带流式传输的消息过滤器（`examples/handoffs/message_filter_streaming.py`）

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    展示如何将托管 MCP（Model Context Protocol）与 OpenAI Responses API 配合使用的示例，包括：

    -   无需审批的简单托管 MCP（`examples/hosted_mcp/simple.py`）
    -   MCP 连接器，例如 Google Calendar（`examples/hosted_mcp/connectors.py`）
    -   具备基于中断的审批的人在回路（`examples/hosted_mcp/human_in_the_loop.py`）
    -   MCP 工具调用的审批通过回调（`examples/hosted_mcp/on_approval.py`）

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    了解如何使用 MCP（Model Context Protocol）构建智能体，包括：

    -   文件系统示例
    -   Git 示例
    -   MCP 提示词服务示例
    -   SSE（Server-Sent Events）示例
    -   SSE 远程服务连接（`examples/mcp/sse_remote_example`）
    -   Streamable HTTP 示例
    -   Streamable HTTP 远程连接（`examples/mcp/streamable_http_remote_example`）
    -   用于 Streamable HTTP 的自定义 HTTP 客户端工厂（`examples/mcp/streamablehttp_custom_client_example`）
    -   使用 `MCPUtil.get_all_function_tools` 预取所有 MCP 工具（`examples/mcp/get_all_mcp_tools_example`）
    -   MCPServerManager 与 FastAPI（`examples/mcp/manager_example`）
    -   MCP 工具过滤（`examples/mcp/tool_filter_example`）

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    智能体不同记忆实现的示例，包括：

    -   SQLite 会话存储
    -   高级 SQLite 会话存储
    -   Redis 会话存储
    -   SQLAlchemy 会话存储
    -   Dapr 状态存储会话存储
    -   加密会话存储
    -   OpenAI Conversations 会话存储
    -   Responses 压缩会话存储
    -   使用 `ModelSettings(store=False)` 的无状态 Responses 压缩（`examples/memory/compaction_session_stateless_example.py`）
    -   基于文件的会话存储（`examples/memory/file_session.py`）
    -   具备人在回路的基于文件的会话（`examples/memory/file_hitl_example.py`）
    -   具备人在回路的 SQLite 内存会话（`examples/memory/memory_session_hitl_example.py`）
    -   具备人在回路的 OpenAI Conversations 会话（`examples/memory/openai_session_hitl_example.py`）
    -   跨会话的 HITL 审批/拒绝场景（`examples/memory/hitl_session_scenario.py`）

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    探索如何将非 OpenAI 模型与 SDK 配合使用，包括自定义提供商和第三方适配器。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    展示如何使用 SDK 构建实时体验的示例，包括：

    -   包含结构化文本和图像消息的 Web 应用模式
    -   命令行音频循环和播放处理
    -   基于 WebSocket 的 Twilio Media Streams 集成
    -   使用 Realtime Calls API 附加流程的 Twilio SIP 集成

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    展示如何处理推理内容的示例，包括：

    -   通过 Runner API 处理推理内容，支持流式传输和非流式传输（`examples/reasoning_content/runner_example.py`）
    -   通过 OpenRouter 使用 OSS 模型处理推理内容（`examples/reasoning_content/gpt_oss_stream.py`）
    -   基础推理内容示例（`examples/reasoning_content/main.py`）

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    一个简单的深度研究克隆，展示复杂的多智能体研究工作流。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    了解如何实现由OpenAI托管的工具以及实验性 Codex 工具，例如：

    -   网络检索，以及带过滤器的网络检索
    -   文件检索
    -   Code interpreter
    -   Apply patch 工具，包含文件编辑和审批（`examples/tools/apply_patch.py`）
    -   带审批回调的 Shell 工具执行（`examples/tools/shell.py`）
    -   具备人在回路基于中断审批的 Shell 工具（`examples/tools/shell_human_in_the_loop.py`）
    -   带内联技能的托管容器 shell（`examples/tools/container_shell_inline_skill.py`）
    -   带技能引用的托管容器 shell（`examples/tools/container_shell_skill_reference.py`）
    -   带本地技能的本地 shell（`examples/tools/local_shell_skill.py`）
    -   具有命名空间和延迟工具的工具搜索（`examples/tools/tool_search.py`）
    -   计算机操作
    -   图像生成
    -   实验性 Codex 工具工作流（`examples/tools/codex.py`）
    -   实验性 Codex 同线程工作流（`examples/tools/codex_same_thread.py`）

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    查看语音智能体示例，使用我们的 TTS 和 STT 模型，包括流式语音示例。