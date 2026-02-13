---
search:
  exclude: true
---
# 发布流程/变更日志

本项目遵循略微修改的语义化版本规范，版本格式为 `0.Y.Z`。开头的 `0` 表示 SDK 仍在快速演进。各部分的递增规则如下：

## 次版本（`Y`）

对于任何未标记为 beta 的公共接口上的**破坏性变更**，我们会递增次版本号 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 可能包含破坏性变更。

如果你不希望出现破坏性变更，我们建议在项目中将版本固定到 `0.0.x`。

## 修订版本（`Z`）

对于非破坏性变更，我们会递增 `Z`：

- Bug 修复
- 新功能
- 私有接口变更
- beta 功能更新

## 破坏性变更日志

### 0.8.0

在此版本中，有两项运行时行为变更可能需要迁移工作：

- 包装**同步** Python 可调用对象的工具调用现在通过 `asyncio.to_thread(...)` 在工作线程上执行，而不是在事件循环线程上运行。如果你的工具逻辑依赖线程本地状态或线程亲和资源，请迁移到异步工具实现，或在你的工具代码中显式声明线程亲和性。
- 本地 MCP 工具的失败处理现在可配置，且默认行为可能返回模型可见的错误输出，而不是让整个运行失败。如果你依赖快速失败（fail-fast）语义，请设置 `mcp_config={"failure_error_function": None}`。服务级别的 `failure_error_function` 值会覆盖智能体级别设置，因此请在每个带有显式处理器的本地 MCP 服务上设置 `failure_error_function=None`。

### 0.7.0

在此版本中，有一些行为变更可能影响现有应用：

- 嵌套任务转移历史现在为**可选启用**（默认禁用）。如果你依赖 v0.6.x 默认的嵌套行为，请显式设置 `RunConfig(nest_handoff_history=True)`。
- `gpt-5.1` / `gpt-5.2` 的默认 `reasoning.effort` 变更为 `"none"`（从之前由 SDK 默认配置的 `"low"` 变更而来）。如果你的提示词或质量/成本配置依赖 `"low"`，请在 `model_settings` 中显式设置。

### 0.6.0

在此版本中，默认的任务转移历史现在会打包为一条 assistant 消息，而不是暴露原始的 user/assistant 轮次，从而为下游智能体提供简洁、可预测的回顾
- 现有的单消息任务转移转录现在默认会在 `<CONVERSATION HISTORY>` 块之前以 "For context, here is the conversation so far between the user and the previous agent:" 开头，从而让下游智能体获得清晰标注的回顾

### 0.5.0

此版本未引入任何可见的破坏性变更，但包含新功能，并在底层进行了若干重要更新：

- 增加对 `RealtimeRunner` 的支持，以处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip)
- 为兼容 Python 3.14，对 `Runner#run_sync` 的内部逻辑进行了重大修订

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 包 v1.x 版本。请将 openai v2.x 与本 SDK 一起使用。

### 0.3.0

在此版本中，Realtime API 支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，原先有些地方以 `Agent` 作为参数，现在改为以 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这纯粹是类型标注变更，你仍会收到 `Agent` 对象。要更新，只需将类型错误处的 `Agent` 替换为 `AgentBase`。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增两个参数：`run_context` 和 `agent`。你需要将这些参数添加到任何继承 `MCPServer` 的类中。