---
search:
  exclude: true
---
# 发布流程/更新日志

本项目遵循经轻微修改的语义化版本规范，采用 `0.Y.Z` 形式。前导的 `0` 表示该 SDK 仍在快速演进中。版本号的递增规则如下：

## 次版本号（`Y`）

对于未标注为 beta 的任何公共接口发生的**破坏性变更**，我们将提升次版本号 `Y`。例如，从 `0.0.x` 到 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在你的项目中固定为 `0.0.x` 版本。

## 修订号（`Z`）

对于非破坏性变更，我们将提升修订号 `Z`：

- Bug 修复
- 新功能
- 私有接口的变更
- beta 功能的更新

## 破坏性变更更新日志

### 0.6.0

此版本中，默认的任务转移历史现已封装为单条 assistant 消息，而不再暴露原始的 user/assistant 轮次，从而为下游智能体提供简洁、可预期的概述。
- 现有的单消息任务转移记录默认以“For context, here is the conversation so far between the user and the previous agent:”开头，随后是 `<CONVERSATION HISTORY>` 块，方便下游智能体获得清晰标注的概述

### 0.5.0

此版本未引入可见的破坏性变更，但包含新功能以及若干重要的底层更新：

- 为 `RealtimeRunner` 增加了对 [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 大幅修订了 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0

此版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请搭配本 SDK 使用 openai v2.x。

### 0.3.0

此版本中，Realtime API 支持迁移至 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

此版本中，若干原本接收 `Agent` 作为参数的场景，现在改为接收 `AgentBase`。例如 MCP 服务中的 `list_tools()` 调用。此变更纯属类型层面，你仍将收到 `Agent` 对象。要更新，只需将类型错误中出现的 `Agent` 替换为 `AgentBase` 即可。

### 0.1.0

此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增两个参数：`run_context` 和 `agent`。你需要在继承 `MCPServer` 的任何类中添加这些参数。