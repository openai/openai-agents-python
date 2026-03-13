---
search:
  exclude: true
---
# 发布流程/变更日志

该项目采用略微修改的语义化版本方案，格式为 `0.Y.Z`。前导 `0` 表示 SDK 仍在快速演进。各部分递增规则如下：

## 次版本（`Y`）

对于任何未标记为 beta 的公共接口发生**破坏性变更**时，我们会提升次版本 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 可能包含破坏性变更。

如果你不希望出现破坏性变更，我们建议在你的项目中固定到 `0.0.x` 版本。

## 补丁版本（`Z`）

对于非破坏性变更，我们会递增 `Z`：

-   Bug 修复
-   新功能
-   私有接口变更
-   beta 功能更新

## 破坏性变更日志

### 0.12.0

此次次版本发布**不**引入破坏性变更。主要功能新增请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)。

### 0.11.0

此次次版本发布**不**引入破坏性变更。主要功能新增请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)。

### 0.10.0

此次次版本发布**不**引入破坏性变更，但为 OpenAI Responses 用户带来了一个重要新功能领域：Responses API 的 websocket 传输支持。

亮点：

-   为 OpenAI Responses 模型新增 websocket 传输支持（可选启用；HTTP 仍为默认传输方式）。
-   新增 `responses_websocket_session()` 辅助函数 / `ResponsesWebSocketSession`，用于在多轮运行中复用支持 websocket 的共享 provider 和 `RunConfig`。
-   新增 websocket 流式传输示例（`examples/basic/stream_ws.py`），涵盖流式传输、tools、审批以及后续轮次。

### 0.9.0

在此版本中，Python 3.9 不再受支持，因为这个主版本已在三个月前到达 EOL。请升级到更新的运行时版本。

此外，`Agent#as_tool()` 方法返回值的类型提示已从 `Tool` 收窄为 `FunctionTool`。此变更通常不会导致破坏性问题，但如果你的代码依赖更宽泛的联合类型，你可能需要在本侧进行一些调整。

### 0.8.0

在此版本中，两项运行时行为变更可能需要迁移工作：

- 工具调用中包装**同步** Python 可调用对象的函数，现在会通过 `asyncio.to_thread(...)` 在工作线程上执行，而不是在事件循环线程上运行。如果你的工具逻辑依赖线程本地状态或线程绑定资源，请迁移到异步工具实现，或在工具代码中显式处理线程绑定。
- 本地 MCP 工具失败处理现已可配置，且默认行为可能返回模型可见的错误输出，而不是让整次运行失败。如果你依赖快速失败语义，请设置 `mcp_config={"failure_error_function": None}`。服务级别的 `failure_error_function` 会覆盖智能体级别设置，因此请在每个具有显式处理器的本地 MCP 服务上设置 `failure_error_function=None`。

### 0.7.0

在此版本中，有几项行为变更可能影响现有应用：

- 嵌套任务转移历史现在为**可选启用**（默认禁用）。如果你依赖 v0.6.x 默认的嵌套行为，请显式设置 `RunConfig(nest_handoff_history=True)`。
- `gpt-5.1` / `gpt-5.2` 的默认 `reasoning.effort` 已更改为 `"none"`（此前为由 SDK 默认值配置的 `"low"`）。如果你的提示词或质量/成本配置依赖 `"low"`，请在 `model_settings` 中显式设置。

### 0.6.0

在此版本中，默认任务转移历史现在会被打包为单条 assistant 消息，而不是暴露原始 user/assistant 轮次，从而为下游智能体提供简洁且可预测的回顾
- 现有的单消息任务转移记录现在默认会在 `<CONVERSATION HISTORY>` 块前加上 “For context, here is the conversation so far between the user and the previous agent:”，从而让下游智能体获得标签清晰的回顾

### 0.5.0

此版本不引入任何可见的破坏性变更，但包含新功能以及一些底层重要更新：

- 新增对 `RealtimeRunner` 的支持，以处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip)
- 为兼容 Python 3.14，显著修订了 `Runner#run_sync` 的内部逻辑

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 包 v1.x 版本。请搭配本 SDK 使用 openai v2.x。

### 0.3.0

在此版本中，Realtime API 支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，少数原本以 `Agent` 作为参数的位置，现在改为以 `AgentBase` 作为参数。例如 MCP 服务中的 `list_tools()` 调用。这是纯类型层面的变更，你仍会收到 `Agent` 对象。更新方式是将 `Agent` 替换为 `AgentBase` 以修复类型错误。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增两个参数：`run_context` 和 `agent`。你需要将这两个参数添加到所有继承 `MCPServer` 的类中。