---
search:
  exclude: true
---
# 发布流程/变更日志

该项目遵循语义化版本控制的轻微变体，采用 `0.Y.Z` 的形式。前导 `0` 表示 SDK 仍在快速演进。各部分按如下方式递增：

## 次版本（`Y`）

对于任何未标记为 beta 的公共接口发生**破坏性变更**时，我们会提升次版本 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 可能包含破坏性变更。

如果你不希望出现破坏性变更，我们建议你在项目中固定到 `0.0.x` 版本。

## 补丁版本（`Z`）

对于非破坏性变更，我们会递增 `Z`：

-   Bug 修复
-   新功能
-   私有接口变更
-   beta 功能更新

## 破坏性变更日志

### 0.13.0

此次次版本发布**不**引入破坏性变更，但包含一项值得关注的 Realtime 默认值更新，以及新的 MCP 能力与运行时稳定性修复。

亮点：

-   默认 websocket Realtime 模型现为 `gpt-realtime-1.5`，因此新的 Realtime 智能体配置无需额外设置即可使用较新模型。
-   `MCPServer` 现公开 `list_resources()`、`list_resource_templates()` 和 `read_resource()`，`MCPServerStreamableHttp` 现公开 `session_id`，以便可流式 HTTP 会话可在重连或无状态 worker 间恢复。
-   Chat Completions 集成现在可通过 `should_replay_reasoning_content` 选择启用推理内容回放，从而改善 LiteLLM/DeepSeek 等适配器在特定提供方上的推理/工具调用连续性。
-   修复了多个运行时和会话边界情况，包括 `SQLAlchemySession` 中并发首次写入、在移除推理内容后压缩请求出现孤立 assistant 消息 ID、`remove_all_tools()` 遗留 MCP/推理项，以及工具调用批量执行器中的竞态问题。

### 0.12.0

此次次版本发布**不**引入破坏性变更。主要功能新增请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)。

### 0.11.0

此次次版本发布**不**引入破坏性变更。主要功能新增请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)。

### 0.10.0

此次次版本发布**不**引入破坏性变更，但为 OpenAI Responses 用户带来了一个重要的新功能领域：Responses API 的 websocket 传输支持。

亮点：

-   为 OpenAI Responses 模型新增 websocket 传输支持（可选启用；默认传输方式仍为 HTTP）。
-   新增 `responses_websocket_session()` 辅助函数 / `ResponsesWebSocketSession`，用于在多轮运行中复用支持 websocket 的共享 provider 与 `RunConfig`。
-   新增 websocket 流式传输示例（`examples/basic/stream_ws.py`），覆盖流式传输、tools、审批和后续轮次。

### 0.9.0

在此版本中，Python 3.9 不再受支持，因为该主版本已在三个月前达到 EOL。请升级到更新的运行时版本。

此外，`Agent#as_tool()` 方法返回值的类型提示已从 `Tool` 收窄为 `FunctionTool`。此变更通常不会导致破坏性问题，但如果你的代码依赖更宽泛的联合类型，可能需要在你这边做一些调整。

### 0.8.0

在此版本中，两项运行时行为变更可能需要迁移工作：

- Function tools 包装的**同步**Python 可调用对象，现在通过 `asyncio.to_thread(...)` 在 worker 线程中执行，而不是在事件循环线程中运行。如果你的工具逻辑依赖线程本地状态或线程亲和资源，请迁移为异步工具实现，或在工具代码中显式处理线程亲和性。
- 本地 MCP 工具失败处理现在可配置，默认行为可能返回模型可见的错误输出，而不是使整次运行失败。如果你依赖快速失败语义，请设置 `mcp_config={"failure_error_function": None}`。服务级 `failure_error_function` 值会覆盖智能体级设置，因此请在每个设置了显式处理器的本地 MCP 服务上设置 `failure_error_function=None`。

### 0.7.0

在此版本中，有一些行为变更可能影响现有应用：

- 嵌套任务转移历史现在为**可选启用**（默认关闭）。如果你依赖 v0.6.x 默认的嵌套行为，请显式设置 `RunConfig(nest_handoff_history=True)`。
- `gpt-5.1` / `gpt-5.2` 的默认 `reasoning.effort` 变更为 `"none"`（此前由 SDK 默认值配置为 `"low"`）。如果你的提示词或质量/成本配置依赖 `"low"`，请在 `model_settings` 中显式设置。

### 0.6.0

在此版本中，默认任务转移历史现会打包为单条 assistant 消息，而不是暴露原始的 user/assistant 轮次，从而为下游智能体提供简洁、可预测的回顾
- 现有的单消息任务转移记录现在默认会在 `<CONVERSATION HISTORY>` 块前以 "For context, here is the conversation so far between the user and the previous agent:" 开头，从而为下游智能体提供清晰标注的回顾

### 0.5.0

此版本不引入可见的破坏性变更，但包含新功能以及一些底层重要更新：

- 新增 `RealtimeRunner` 对 [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 为兼容 Python 3.14，显著修订了 `Runner#run_sync` 的内部逻辑

### 0.4.0

在此版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请配合本 SDK 使用 openai v2.x。

### 0.3.0

在此版本中，Realtime API 支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，过去一些以 `Agent` 作为参数的位置，现在改为以 `AgentBase` 作为参数。例如 MCP 服务中的 `list_tools()` 调用。这是纯类型层面的变更，你仍会收到 `Agent` 对象。更新方式是将类型错误中的 `Agent` 替换为 `AgentBase`。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增两个参数：`run_context` 和 `agent`。你需要将这些参数添加到所有继承 `MCPServer` 的类中。