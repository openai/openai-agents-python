---
search:
  exclude: true
---
# 发布流程/变更日志

该项目遵循稍作修改的语义化版本控制，格式为 `0.Y.Z`。前导的 `0` 表示 SDK 仍在快速演进中。各部分的递增规则如下：

## 次版本（`Y`）

对于任何未标记为 beta 的公开接口上的**破坏性变更**，我们会提升次版本 `Y`。例如，从 `0.0.x` 到 `0.1.x` 可能包含破坏性变更。

如果你不希望出现破坏性变更，我们建议你在项目中锁定到 `0.0.x` 版本。

## 补丁版本（`Z`）

对于非破坏性变更，我们会递增 `Z`：

- Bug 修复
- 新功能
- 私有接口的变更
- beta 功能的更新

## 破坏性变更日志

### 0.14.0

这个次版本**不会**引入破坏性变更，但新增了一个重要的 beta 功能领域：Sandbox Agents，以及在本地、容器化和托管环境中使用它们所需的运行时、后端和文档支持。

亮点：

- 新增了以 `SandboxAgent`、`Manifest` 和 `SandboxRunConfig` 为核心的 beta 沙箱运行时接口，使智能体能够在持久化的隔离工作区中运行，并支持文件、目录、Git 仓库、挂载、快照和恢复功能。
- 新增了适用于本地和容器化开发的沙箱执行后端，通过 `UnixLocalSandboxClient` 和 `DockerSandboxClient` 提供；同时还通过可选扩展提供了对 Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop 和 Vercel 托管提供方的集成。
- 新增了沙箱记忆支持，使未来运行可以复用之前运行中的经验，支持渐进式披露、多轮分组、可配置的隔离边界，以及包括基于 S3 工作流在内的持久化记忆示例。
- 新增了更广泛的工作区与恢复模型，包括本地和合成工作区条目、适用于 S3/R2/GCS/Azure Blob Storage/S3 Files 的远程存储挂载、可移植快照，以及通过 `RunState`、`SandboxSessionState` 或保存的快照进行恢复的流程。
- 在 `examples/sandbox/` 下新增了大量沙箱示例和教程，涵盖带技能的编码任务、任务转移、记忆、特定提供方配置，以及代码审查、数据室问答和网站克隆等端到端工作流。
- 扩展了核心运行时和追踪栈，加入了具备沙箱感知能力的会话准备、能力绑定、状态序列化、统一追踪、提示缓存键默认值，以及对敏感 MCP 输出更安全的脱敏处理。

### 0.13.0

这个次版本**不会**引入破坏性变更，但包含了一项值得注意的 Realtime 默认更新，以及新的 MCP 能力和运行时稳定性修复。

亮点：

- 默认的 websocket Realtime 模型现为 `gpt-realtime-1.5`，因此新的 Realtime 智能体配置无需额外设置即可使用更新的模型。
- `MCPServer` 现在公开 `list_resources()`、`list_resource_templates()` 和 `read_resource()`，而 `MCPServerStreamableHttp` 现在公开 `session_id`，因此可流式 HTTP 会话可以在重新连接或无状态工作进程之间恢复。
- Chat Completions 集成现在可以通过 `should_replay_reasoning_content` 选择启用推理内容重放，从而改善 LiteLLM/DeepSeek 等适配器中针对特定提供方的推理/工具调用连续性。
- 修复了多个运行时和会话边界情况，包括 `SQLAlchemySession` 中并发首次写入、推理内容剥离后带有孤立 assistant message ID 的压缩请求、`remove_all_tools()` 遗留 MCP/推理项，以及工具调用批量执行器中的竞争问题。

### 0.12.0

这个次版本**不会**引入破坏性变更。有关主要功能新增内容，请参阅[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)。

### 0.11.0

这个次版本**不会**引入破坏性变更。有关主要功能新增内容，请参阅[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)。

### 0.10.0

这个次版本**不会**引入破坏性变更，但为 OpenAI Responses 用户带来了一个重要的新功能领域：Responses API 的 websocket 传输支持。

亮点：

- 为 OpenAI Responses 模型新增了 websocket 传输支持（选择启用；HTTP 仍然是默认传输方式）。
- 新增了 `responses_websocket_session()` 辅助函数 / `ResponsesWebSocketSession`，用于在多轮运行中复用共享的支持 websocket 的提供方和 `RunConfig`。
- 新增了一个 websocket 流式传输示例（`examples/basic/stream_ws.py`），涵盖流式传输、tools、审批和后续轮次。

### 0.9.0

在此版本中，Python 3.9 不再受支持，因为这个主版本已在三个月前达到 EOL。请升级到更新的运行时版本。

此外，`Agent#as_tool()` 方法返回值的类型提示已从 `Tool` 收窄为 `FunctionTool`。此变更通常不会导致破坏性问题，但如果你的代码依赖更宽泛的联合类型，你可能需要在代码侧进行一些调整。

### 0.8.0

在此版本中，两项运行时行为变更可能需要进行迁移工作：

- 包装**同步** Python 可调用对象的工具调用，现在会通过 `asyncio.to_thread(...)` 在工作线程上执行，而不再运行在事件循环线程上。如果你的工具逻辑依赖线程局部状态或线程绑定资源，请迁移到异步工具实现，或在工具代码中显式处理线程绑定。
- 本地 MCP 工具失败处理现在可配置，且默认行为可能会返回模型可见的错误输出，而不是让整个运行失败。如果你依赖快速失败语义，请设置 `mcp_config={"failure_error_function": None}`。服务级别的 `failure_error_function` 值会覆盖智能体级别设置，因此请在每个具有显式处理器的本地 MCP 服务上设置 `failure_error_function=None`。

### 0.7.0

在此版本中，有一些行为变更可能会影响现有应用：

- 嵌套任务转移历史现在为**选择启用**（默认禁用）。如果你依赖 v0.6.x 默认的嵌套行为，请显式设置 `RunConfig(nest_handoff_history=True)`。
- `gpt-5.1` / `gpt-5.2` 的默认 `reasoning.effort` 已改为 `"none"`（此前由 SDK 默认值配置为 `"low"`）。如果你的提示词或质量/成本配置依赖 `"low"`，请在 `model_settings` 中显式设置。

### 0.6.0

在此版本中，默认的任务转移历史现在会被打包为单条 assistant 消息，而不是暴露原始的用户/assistant 轮次，从而为下游智能体提供简洁、可预测的回顾
- 现有的单条消息任务转移记录现在默认会在 `<CONVERSATION HISTORY>` 块之前以 "For context, here is the conversation so far between the user and the previous agent:" 开头，从而让下游智能体获得带有清晰标签的回顾

### 0.5.0

此版本不会引入任何可见的破坏性变更，但包含了新功能和一些底层的重要更新：

- 新增对 `RealtimeRunner` 处理[SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip)的支持
- 为兼容 Python 3.14，大幅修改了 `Runner#run_sync` 的内部逻辑

### 0.4.0

在此版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请将 openai v2.x 与此 SDK 一起使用。

### 0.3.0

在此版本中，Realtime API 支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，一些原本接收 `Agent` 作为参数的位置，现在改为接收 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这纯粹是类型层面的变更，你仍然会收到 `Agent` 对象。要完成更新，只需将 `Agent` 替换为 `AgentBase` 以修复类型错误。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。你需要将这两个参数添加到任何继承 `MCPServer` 的类中。