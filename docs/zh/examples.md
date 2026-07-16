---
search:
  exclude: true
---
# 代码示例

请查看[代码仓库](https://github.com/openai/openai-agents-python/tree/main/examples)的 examples 部分，了解 SDK 的各种示例实现。这些示例分为多个目录，展示了不同的模式和功能。

## 目录

- **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns)：**此目录中的示例展示了常见的智能体设计模式，例如

    -   确定性工作流
    -   Agents as tools
    -   具有流式事件的Agents as tools（`examples/agent_patterns/agents_as_tools_streaming.py`）
    -   具有结构化输入参数的Agents as tools（`examples/agent_patterns/agents_as_tools_structured.py`）
    -   并行执行智能体
    -   按条件使用工具
    -   以不同的行为强制使用工具（`examples/agent_patterns/forcing_tool_use.py`）
    -   输入/输出安全防护措施
    -   由 LLM 充当评判者
    -   路由
    -   流式安全防护措施
    -   采用工具审批和状态序列化的人机协同（`examples/agent_patterns/human_in_the_loop.py`）
    -   采用流式传输的人机协同（`examples/agent_patterns/human_in_the_loop_stream.py`）
    -   审批流程的自定义拒绝消息（`examples/agent_patterns/human_in_the_loop_custom_rejection.py`）

- **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic)：**这些示例展示了 SDK 的基础功能，例如

    -   Hello world 示例（默认模型、GPT-5、开放权重模型）
    -   智能体生命周期管理
    -   运行钩子和智能体钩子的生命周期示例（`examples/basic/lifecycle_example.py`）
    -   动态系统提示词
    -   基本工具使用方式（`examples/basic/tools.py`）
    -   工具输入/输出安全防护措施（`examples/basic/tool_guardrails.py`）
    -   图像工具输出（`examples/basic/image_tool_output.py`）
    -   流式输出（文本、项目、函数调用参数）
    -   使用跨轮次共享会话辅助程序的 Responses WebSocket 传输（`examples/basic/stream_ws.py`）
    -   提示词模板
    -   文件处理（本地和远程、图像和 PDF）
    -   使用量追踪
    -   由 Runner 管理的重试设置（`examples/basic/retry.py`）
    -   通过第三方适配器进行由 Runner 管理的重试（`examples/basic/retry_litellm.py`）
    -   非严格输出类型
    -   前一个响应 ID 的使用方式

- **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service)：**航空公司客户服务系统示例。

- **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent)：**金融研究智能体，展示了使用智能体和工具进行金融数据分析的结构化研究工作流。

- **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs)：**包含消息筛选的智能体任务转移实用示例，包括：

    -   消息筛选器示例（`examples/handoffs/message_filter.py`）
    -   采用流式传输的消息筛选器（`examples/handoffs/message_filter_streaming.py`）

- **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp)：**展示如何将托管式 MCP（Model Context Protocol）与 OpenAI Responses API 配合使用的示例，包括：

    -   无需审批的简单托管式 MCP（`examples/hosted_mcp/simple.py`）
    -   Google Calendar 等 MCP 连接器（`examples/hosted_mcp/connectors.py`）
    -   采用基于中断审批的人机协同（`examples/hosted_mcp/human_in_the_loop.py`）
    -   MCP 工具调用的审批时回调（`examples/hosted_mcp/on_approval.py`）

- **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp)：**了解如何使用 MCP（Model Context Protocol）构建智能体，包括：

    -   文件系统示例
    -   Git 示例
    -   MCP 提示词服务示例
    -   SSE（服务器发送事件）示例
    -   SSE 远程服务连接（`examples/mcp/sse_remote_example`）
    -   可流式传输的 HTTP 示例
    -   可流式传输的 HTTP 远程连接（`examples/mcp/streamable_http_remote_example`）
    -   用于可流式传输 HTTP 的自定义 HTTP 客户端工厂（`examples/mcp/streamablehttp_custom_client_example`）
    -   使用 `MCPUtil.get_all_function_tools` 预取所有 MCP 工具（`examples/mcp/get_all_mcp_tools_example`）
    -   搭配 FastAPI 使用 MCPServerManager（`examples/mcp/manager_example`）
    -   MCP 工具筛选（`examples/mcp/tool_filter_example`）

- **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory)：**不同智能体记忆实现的示例，包括：

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
    -   采用人机协同的基于文件会话（`examples/memory/file_hitl_example.py`）
    -   采用人机协同的 SQLite 内存会话（`examples/memory/memory_session_hitl_example.py`）
    -   采用人机协同的 OpenAI Conversations 会话（`examples/memory/openai_session_hitl_example.py`）
    -   跨会话的 HITL 审批/拒绝场景（`examples/memory/hitl_session_scenario.py`）

- **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers)：**探索如何将非 OpenAI 模型与 SDK 配合使用，包括自定义提供商和第三方适配器。

- **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)：**展示如何使用 SDK 构建实时体验的示例，包括：

    -   使用结构化文本和图像消息的 Web 应用模式
    -   命令行音频循环和播放处理
    -   通过 WebSocket 集成 Twilio Media Streams
    -   使用 Realtime Calls API 附加流程集成 Twilio SIP

- **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content)：**展示如何处理推理内容的示例，包括：

    -   通过 Runner API 处理推理内容，包括流式和非流式方式（`examples/reasoning_content/runner_example.py`）
    -   通过 OpenRouter 使用 OSS 模型处理推理内容（`examples/reasoning_content/gpt_oss_stream.py`）
    -   基本推理内容示例（`examples/reasoning_content/main.py`）

- **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot)：**简单的深度研究复刻版本，展示了复杂的多智能体研究工作流。

- **[sandbox](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox)：**在隔离工作区中运行智能体的示例，包括：

    -   基本沙箱智能体设置（`examples/sandbox/basic.py`）
    -   Unix 本地和 Docker 沙箱生命周期示例
    -   由沙箱支持的任务转移（`examples/sandbox/handoffs.py`）
    -   沙箱记忆和快照恢复（`examples/sandbox/memory.py`）
    -   作为工具公开的沙箱智能体（`examples/sandbox/sandbox_agents_as_tools.py`）

- **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools)：**了解如何实现由OpenAI托管的工具和实验性 Codex 工具，例如：

    -   网络检索和带筛选条件的网络检索
    -   文件检索
    -   Code interpreter
    -   具备文件编辑和审批功能的补丁应用工具（`examples/tools/apply_patch.py`）
    -   具有审批回调的 Shell 工具执行（`examples/tools/shell.py`）
    -   采用基于中断的人机协同审批的 Shell 工具（`examples/tools/shell_human_in_the_loop.py`）
    -   具有内联技能的托管容器 Shell（`examples/tools/container_shell_inline_skill.py`）
    -   具有技能引用的托管容器 Shell（`examples/tools/container_shell_skill_reference.py`）
    -   具有本地技能的本地 Shell（`examples/tools/local_shell_skill.py`）
    -   使用命名空间和延迟工具的工具搜索（`examples/tools/tool_search.py`）
    -   计算机操作
    -   图像生成
    -   实验性 Codex 工具工作流（`examples/tools/codex.py`）
    -   实验性 Codex 同线程工作流（`examples/tools/codex_same_thread.py`）

- **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice)：**查看使用我们的 TTS 和 STT 模型构建语音智能体的示例，包括流式语音示例。