---
search:
  exclude: true
---
# 示例

请在 [repo](https://github.com/openai/openai-agents-python/tree/main/examples) 的 examples 部分查看 SDK 的多种 sample code 实现。这些示例按多个目录组织，展示了不同的模式和能力。

## 目录

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    此目录中的示例说明了常见的智能体设计模式，例如：

    -   确定性工作流
    -   Agents as tools
    -   并行智能体执行
    -   条件式工具使用
    -   输入/输出安全防护措施
    -   LLM 作为评审
    -   路由
    -   流式传输安全防护措施

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    这些示例展示了 SDK 的基础能力，例如：

    -   Hello World 示例（默认模型、GPT-5、开放权重模型）
    -   智能体生命周期管理
    -   动态系统提示词
    -   流式传输输出（文本、条目、函数调用参数）
    -   跨轮次使用共享会话辅助器的 Responses websocket 传输（`examples/basic/stream_ws.py`）
    -   提示词模板
    -   文件处理（本地和远程、图像和 PDF）
    -   用量追踪
    -   非严格输出类型
    -   先前响应 ID 的使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    面向航空公司的客户服务系统示例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    一个金融研究智能体，演示了使用智能体和工具进行金融数据分析的结构化研究工作流。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    查看带有消息过滤的智能体任务转移实践示例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    演示如何使用托管 MCP（Model context protocol）连接器和审批的示例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    了解如何使用 MCP（Model context protocol）构建智能体，包括：

    -   文件系统示例
    -   Git 示例
    -   MCP 提示词服务示例
    -   SSE（服务端发送事件）示例
    -   可流式 HTTP 示例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    不同智能体记忆实现的示例，包括：

    -   SQLite 会话存储
    -   高级 SQLite 会话存储
    -   Redis 会话存储
    -   SQLAlchemy 会话存储
    -   Dapr 状态存储会话存储
    -   加密会话存储
    -   OpenAI Conversations 会话存储
    -   Responses 压缩会话存储

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    探索如何在 SDK 中使用非 OpenAI 模型，包括自定义提供方和 LiteLLM 集成。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    展示如何使用 SDK 构建实时体验的示例，包括：

    -   使用结构化文本和图像消息的 Web 应用模式
    -   命令行音频循环与播放处理
    -   通过 WebSocket 集成 Twilio Media Streams
    -   使用 Realtime Calls API 附加流程进行 Twilio SIP 集成

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    演示如何处理推理内容和 structured outputs 的示例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    简单的深度研究克隆，演示了复杂的多智能体研究工作流。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    了解如何实现由OpenAI托管的工具和实验性的 Codex 工具能力，例如：

    -   网络检索以及带过滤器的网络检索
    -   文件检索
    -   Code Interpreter
    -   带内联技能的托管容器 shell（`examples/tools/container_shell_inline_skill.py`）
    -   带技能引用的托管容器 shell（`examples/tools/container_shell_skill_reference.py`）
    -   计算机操作
    -   图像生成
    -   实验性 Codex 工具工作流（`examples/tools/codex.py`）
    -   实验性 Codex 同线程工作流（`examples/tools/codex_same_thread.py`）

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    查看语音智能体示例，使用我们的 TTS 和 STT 模型，包括流式语音示例。