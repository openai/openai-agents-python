---
search:
  exclude: true
---
# 发布流程/变更日志

该项目遵循一种略微修改过的语义化版本规范，格式为 `0.Y.Z`。开头的 `0` 表示 SDK 仍在快速演进。各组件的递增规则如下：

## 次版本（`Y`）

对于任何未标记为 beta 的公共接口的**破坏性变更**，我们会递增次版本号 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，我们建议在项目中将版本固定在 `0.0.x`。

## 补丁版本（`Z`）

对于非破坏性变更，我们会递增 `Z`：

- Bug 修复
- 新功能
- 对私有接口的更改
- 对 beta 功能的更新

## 破坏性变更变更日志

### 0.10.0

此次数版本发布**不会**引入破坏性变更，但它为 OpenAI Responses 用户带来了一个重要的新功能领域：Responses API 的 websocket 传输支持。

要点：

- 为 OpenAI Responses 模型新增 websocket 传输支持（可选启用；HTTP 仍为默认传输方式）。
- 新增 `responses_websocket_session()` 辅助函数 / `ResponsesWebSocketSession`，用于在多轮运行中复用同一个支持 websocket 的 provider 与 `RunConfig`。
- 新增一个 websocket 流式传输示例（`examples/basic/stream_ws.py`），涵盖 流式传输、工具、审批以及后续轮次。

### 0.9.0

在此版本中，不再支持 Python 3.9，因为该主要版本已在三个月前到达 EOL。请升级到更新的运行时版本。

此外，`Agent#as_tool()` 方法返回值的类型提示已从 `Tool` 收窄为 `FunctionTool`。这通常不会造成破坏性问题，但如果你的代码依赖更宽的联合类型，你可能需要在你这边做一些调整。

### 0.8.0

在此版本中，两处运行时行为变化可能需要迁移工作：

- 包装**同步** Python 可调用对象的工具调用现在会通过 `asyncio.to_thread(...)` 在工作线程上执行，而不是在事件循环线程中运行。如果你的工具逻辑依赖线程本地状态或线程亲和资源，请迁移到异步工具实现，或在工具代码中显式处理线程亲和性。
- 本地 MCP 工具的失败处理现在可配置，且默认行为可以返回对模型可见的错误输出，而不是让整个运行失败。如果你依赖快速失败（fail-fast）语义，请设置 `mcp_config={"failure_error_function": None}`。服务级别的 `failure_error_function` 值会覆盖智能体级别的设置，因此请在每个带有显式处理器的本地 MCP 服务上设置 `failure_error_function=None`。

### 0.7.0

在此版本中，有一些行为变化可能影响现有应用：

- 嵌套的任务转移历史现在为**可选启用**（默认禁用）。如果你依赖 v0.6.x 默认的嵌套行为，请显式设置 `RunConfig(nest_handoff_history=True)`。
- `gpt-5.1` / `gpt-5.2` 的默认 `reasoning.effort` 改为 `"none"`（此前由 SDK 默认配置的默认值是 `"low"`）。如果你的提示词或质量/成本配置依赖 `"low"`，请在 `model_settings` 中显式设置。

### 0.6.0

在此版本中，默认的任务转移历史现在会被打包为单条 assistant 消息，而不是暴露原始的 user/assistant 轮次，从而为下游智能体提供简洁、可预测的回顾
- 现有的单消息任务转移记录现在默认会在 `<CONVERSATION HISTORY>` 块之前以 "For context, here is the conversation so far between the user and the previous agent:" 开头，从而让下游智能体获得清晰标注的回顾

### 0.5.0

此版本不会引入任何可见的破坏性变更，但它包含新功能以及一些底层的重要更新：

- 新增对 `RealtimeRunner` 的支持，以处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip)
- 为兼容 Python 3.14，显著修订了 `Runner#run_sync` 的内部逻辑

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 包的 v1.x 版本。请使用 openai v2.x 并搭配此 SDK。

### 0.3.0

在此版本中，Realtime API 支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，若干过去以 `Agent` 作为参数的位置，现在改为以 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这仅是类型层面的变更，你仍会收到 `Agent` 对象。更新方式是：将 `Agent` 替换为 `AgentBase` 以修复类型错误。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。你需要将这些参数添加到所有继承 `MCPServer` 的类中。