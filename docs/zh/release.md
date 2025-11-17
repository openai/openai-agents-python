---
search:
  exclude: true
---
# 发布流程/更新日志

该项目遵循经轻微修改的语义化版本规范，形式为 `0.Y.Z`。前导的 `0` 表示该 SDK 仍在快速演进中。各部分的递增规则如下：

## 次版本（`Y`）

对于未标记为 beta 的任何公共接口出现的**破坏性变更**，我们将提升次版本 `Y`。例如，从 `0.0.x` 到 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在你的项目中固定到 `0.0.x` 版本。

## 补丁（`Z`）

对于非破坏性变更，我们将提升 `Z`：

- Bug 修复
- 新功能
- 私有接口的更改
- beta 功能的更新

## 破坏性变更更新日志

### 0.6.0

在此版本中，默认的任务转移历史现在被打包为单条助手消息，而不再暴露原始的用户/助手轮次，为下游智能体提供简明、可预测的回顾
- 现有的单消息任务转移记录现在默认以“For context, here is the conversation so far between the user and the previous agent:”开头，位于 `<CONVERSATION HISTORY>` 块之前，以便下游智能体获得清晰标注的回顾

### 0.5.0

此版本未引入可见的破坏性变更，但包含新功能以及若干重要的底层更新：

- 为 `RealtimeRunner` 增加了处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 为兼容 Python 3.14，对 `Runner#run_sync` 的内部逻辑进行了重大修订

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 包 v1.x 版本。请搭配本 SDK 使用 openai v2.x。

### 0.3.0

在此版本中，Realtime API 的支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，一些过去接收 `Agent` 作为参数的地方，现在改为接收 `AgentBase`。例如 MCP 服务中的 `list_tools()` 调用。此更改仅涉及类型，你仍将收到 `Agent` 对象。要更新，只需将类型错误中的 `Agent` 替换为 `AgentBase` 即可。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增两个参数：`run_context` 和 `agent`。你需要将这些参数添加到任何继承 `MCPServer` 的类中。