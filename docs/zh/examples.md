---
search:
  exclude: true
---
# 代码示例

请查看[仓库](https://github.com/openai/openai-agents-python/tree/main/examples)的 examples 目录，了解 SDK 的各种示例实现。这些代码示例分为多个目录，展示了不同的模式和功能。

## 目录

- **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):** 此目录中的代码示例展示了常见的智能体设计模式，例如

    -   确定性工作流
    -   Agents as tools
    -   具备流式传输事件的Agents as tools（`examples/agent_patterns/agents_as_tools_streaming.py`）
    -   具备结构化输入参数的Agents as tools（`examples/agent_patterns/agents_as_tools_structured.py`）
    -   并行执行智能体
    -   有条件地使用工具
    -   以不同的行为强制使用工具（`examples/agent_patterns/forcing_tool_use.py`）
    -   输入/输出安全防护措施
    -   LLM作为评审
    -   路由
    -   流式传输安全防护措施
    -   通过工具审批和状态序列化实现人机协同（`examples/agent_patterns/human_in_the_loop.py`）
    -   通过流式传输实现人机协同（`examples/agent_patterns/human_in_the_loop_stream.py`）
    -   审批流程的自定义拒绝消息（`examples/agent_patterns/human_in_the_loop_custom_rejection.py`）

- **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):** 这些代码示例展示了 SDK 的基础功能，例如

    -   Hello world代码示例（默认模型、GPT-5、开放权重模型）
    -   智能体生命周期管理
    -   运行钩子和智能体钩子的生命周期代码示例（`examples/basic/lifecycle_example.py`）
    -   动态系统提示词
    -   基础工具使用（`examples/basic/tools.py`）
    -   工具输入/输出安全防护措施（`examples/basic/tool_guardrails.py`）
    -   图像工具输出（`examples/basic/image_tool_output.py`）
    -   流式传输输出（文本、项目、函数调用参数）
    -   使用跨轮次共享会话辅助工具的 Responses WebSocket 传输（`examples/basic/stream_ws.py`）
    -   提示词模板
    -   文件处理（本地和远程文件、图像和 PDF）
    -   用量追踪
    -   由 Runner 管理的重试设置（`examples/basic/retry.py`）
    -   通过第三方适配器实现由 Runner 管理的重试（`examples/basic/retry_litellm.py`）
    -   非严格输出类型
    -   上一响应 ID 的使用

- **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):** 航空公司客户服务系统代码示例。

- **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):** 一个金融研究智能体，展示了使用智能体和工具进行金融数据分析的结构化研究工作流。

- **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):** 包含消息过滤功能的智能体任务转移实用代码示例，包括：

    -   消息过滤器代码示例（`examples/handoffs/message_filter.py`）
    -   采用流式传输的消息过滤器（`examples/handoffs/message_filter_streaming.py`）

- **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):** 展示如何结合OpenAI Responses API 使用托管式MCP（Model Context Protocol）的代码示例，包括：

    -   无需审批的简单托管式MCP（`examples/hosted_mcp/simple.py`）
    -   Google Calendar 等MCP连接器（`examples/hosted_mcp/connectors.py`）
    -   通过基于中断的审批实现人机协同（`examples/hosted_mcp/human_in_the_loop.py`）
    -   MCP工具调用的审批回调（`examples/hosted_mcp/on_approval.py`）

- **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):** 了解如何使用MCP（Model Context Protocol）构建智能体，包括：

    -   文件系统代码示例
    -   Git 代码示例
    -   MCP提示词服务代码示例
    -   SSE（服务发送事件）代码示例
    -   SSE 远程服务连接（`examples/mcp/sse_remote_example`）
    -   可流式传输 HTTP 代码示例
    -   可流式传输 HTTP 远程连接（`examples/mcp/streamable_http_remote_example`）
    -   用于可流式传输 HTTP 的自定义 HTTP 客户端工厂（`examples/mcp/streamablehttp_custom_client_example`）
    -   使用 `MCPUtil.get_all_function_tools` 预取全部MCP工具（`examples/mcp/get_all_mcp_tools_example`）
    -   结合 FastAPI 使用MCPServerManager（`examples/mcp/manager_example`）
    -   MCP工具过滤（`examples/mcp/tool_filter_example`）

- **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):** 智能体不同内存实现的代码示例，包括：

    -   SQLite 会话存储
    -   高级 SQLite 会话存储
    -   Redis 会话存储
    -   SQLAlchemy 会话存储
    -   Dapr 状态存储会话存储
    -   加密会话存储
    -   OpenAI Conversations会话存储
    -   Responses 压缩会话存储
    -   使用 `ModelSettings(store=False)` 的无状态 Responses 压缩（`examples/memory/compaction_session_stateless_example.py`）
    -   基于文件的会话存储（`examples/memory/file_session.py`）
    -   支持人机协同的基于文件的会话（`examples/memory/file_hitl_example.py`）
    -   支持人机协同的 SQLite 内存会话（`examples/memory/memory_session_hitl_example.py`）
    -   支持人机协同的OpenAI Conversations会话（`examples/memory/openai_session_hitl_example.py`）
    -   跨会话的 HITL 审批/拒绝场景（`examples/memory/hitl_session_scenario.py`）

- **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):** 探索如何在 SDK 中使用非OpenAI模型，包括自定义提供商和第三方适配器。

- **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):** 展示如何使用 SDK 构建实时体验的代码示例，包括：

    -   使用结构化文本和图像消息的 Web 应用模式
    -   命令行音频循环和播放处理
    -   通过 WebSocket 集成 Twilio Media Streams
    -   使用 Realtime Calls API 附加流程的 Twilio SIP 集成

- **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):** 展示如何处理推理内容的代码示例，包括：

    -   使用 Runner API 处理推理内容，支持流式传输与非流式传输（`examples/reasoning_content/runner_example.py`）
    -   通过 OpenRouter 使用 OSS 模型处理推理内容（`examples/reasoning_content/gpt_oss_stream.py`）
    -   基础推理内容代码示例（`examples/reasoning_content/main.py`）

- **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):** 简单的深度研究复刻项目，展示了复杂的多智能体研究工作流。

- **[sandbox](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox):** 在隔离工作区中运行智能体的代码示例，包括：

    -   基础沙箱智能体设置（`examples/sandbox/basic.py`）
    -   Unix 本地沙箱和 Docker 沙箱的生命周期代码示例
    -   基于沙箱的任务转移（`examples/sandbox/handoffs.py`）
    -   沙箱内存和快照恢复（`examples/sandbox/memory.py`）
    -   作为工具公开的沙箱智能体（`examples/sandbox/sandbox_agents_as_tools.py`）

- **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):** 了解如何实现由OpenAI托管的工具和实验性 Codex 工具功能，例如：

    -   网络检索以及带筛选条件的网络检索
    -   文件检索
    -   Code interpreter
    -   具备文件编辑和审批功能的补丁应用工具（`examples/tools/apply_patch.py`）
    -   使用审批回调执行 Shell 工具（`examples/tools/shell.py`）
    -   通过基于中断的审批实现人机协同的 Shell 工具（`examples/tools/shell_human_in_the_loop.py`）
    -   具备内联技能的托管容器 Shell（`examples/tools/container_shell_inline_skill.py`）
    -   具备技能引用的托管容器 Shell（`examples/tools/container_shell_skill_reference.py`）
    -   具备本地技能的本地 Shell（`examples/tools/local_shell_skill.py`）
    -   具备命名空间和延迟加载工具的工具搜索（`examples/tools/tool_search.py`）
    -   支持并发结构化工具调用的程序化工具调用（`examples/tools/programmatic_tool_calling.py`）
    -   计算机操作
    -   图像生成
    -   实验性 Codex 工具工作流（`examples/tools/codex.py`）
    -   实验性 Codex 同一线程工作流（`examples/tools/codex_same_thread.py`）

- **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):** 查看使用我们的 TTS 和 STT 模型构建语音智能体的代码示例，包括流式语音代码示例。