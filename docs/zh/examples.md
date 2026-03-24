---
search:
  exclude: true
---
# 示例

请在 [repo](https://github.com/openai/openai-agents-python/tree/main/examples) 的示例部分查看 SDK 的多种 sample code。这些示例按多个目录组织，用于展示不同的模式与能力。

## 目录

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    此目录中的示例展示了常见的智能体设计模式，例如

    -   确定性工作流
    -   Agents as tools
    -   并行智能体执行
    -   条件化工具使用
    -   输入/输出安全防护措施
    -   LLM 作为评审
    -   路由
    -   流式传输安全防护措施
    -   审批流程的自定义拒绝消息（`examples/agent_patterns/human_in_the_loop_custom_rejection.py`）

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    这些示例展示了 SDK 的基础能力，例如

    -   Hello World 示例（默认模型、GPT-5、开源权重模型）
    -   智能体生命周期管理
    -   动态系统提示词
    -   流式传输输出（文本、条目、函数调用参数）
    -   跨多轮共享会话辅助器的 Responses websocket 传输（`examples/basic/stream_ws.py`）
    -   提示词模板
    -   文件处理（本地与远程、图像与 PDF）
    -   用量追踪
    -   Runner 管理的重试设置（`examples/basic/retry.py`）
    -   通过 LiteLLM 使用 Runner 管理的重试（`examples/basic/retry_litellm.py`）
    -   非严格输出类型
    -   上一个 response ID 的用法

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空公司客户服务系统示例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    一个金融研究智能体，展示了用于金融数据分析的、结合智能体与工具的结构化研究工作流。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    查看带有消息过滤的智能体任务转移实践示例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    展示如何使用托管 MCP（Model context protocol）连接器和审批流程的示例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    了解如何基于 MCP（Model context protocol）构建智能体，包括：

    -   文件系统示例
    -   Git 示例
    -   MCP 提示词服务示例
    -   SSE（服务端发送事件）示例
    -   可流式 HTTP 示例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    智能体的不同内存实现示例，包括：

    -   SQLite 会话存储
    -   高级 SQLite 会话存储
    -   Redis 会话存储
    -   SQLAlchemy 会话存储
    -   Dapr 状态存储会话存储
    -   加密会话存储
    -   OpenAI Conversations 会话存储
    -   Responses 压缩会话存储
    -   使用 `ModelSettings(store=False)` 的无状态 Responses 压缩（`examples/memory/compaction_session_stateless_example.py`）

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    探索如何在 SDK 中使用非 OpenAI 模型，包括自定义提供方和 LiteLLM 集成。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    展示如何使用 SDK 构建实时体验的示例，包括：

    -   使用结构化文本与图像消息的 Web 应用模式
    -   命令行音频循环与播放处理
    -   基于 WebSocket 的 Twilio Media Streams 集成
    -   使用 Realtime Calls API 附加流程的 Twilio SIP 集成

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    展示如何处理推理内容与 structured outputs 的示例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    简单的深度研究克隆，展示复杂的多智能体研究工作流。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    了解如何实现由OpenAI托管的工具和实验性 Codex 工具能力，例如：

    -   网络检索及带过滤器的网络检索
    -   文件检索
    -   代码解释器
    -   带内联技能的托管容器 shell（`examples/tools/container_shell_inline_skill.py`）
    -   带技能引用的托管容器 shell（`examples/tools/container_shell_skill_reference.py`）
    -   带本地技能的本地 shell（`examples/tools/local_shell_skill.py`）
    -   带命名空间和延迟工具的工具检索（`examples/tools/tool_search.py`）
    -   计算机操作
    -   图像生成
    -   实验性 Codex 工具工作流（`examples/tools/codex.py`）
    -   实验性 Codex 同线程工作流（`examples/tools/codex_same_thread.py`）

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    查看语音智能体示例，使用我们的 TTS 和 STT 模型，包括流式语音示例。