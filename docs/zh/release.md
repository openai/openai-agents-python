---
search:
  exclude: true
---
# 发布流程/更新日志

本项目遵循经轻微修改的语义化版本规范，版本号形式为 `0.Y.Z`。前导的 `0` 表示该 SDK 仍在快速演进中。各部分递增规则如下：

## 次版本号（`Y`）

对于未标注为 beta 的任何公共接口的**不兼容变更**，我们会提升次版本号 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 可能包含不兼容变更。

如果你不希望引入不兼容变更，建议在你的项目中将版本固定到 `0.0.x`。

## 修订号（`Z`）

对于非破坏性变更，我们会递增 `Z`：

- Bug 修复
- 新功能
- 对私有接口的更改
- 对 beta 功能的更新

## 不兼容变更日志

### 0.6.0

此版本中，默认的任务转移（handoff）历史现在被打包为单条助手消息，而不是暴露原始的用户/助手轮次，从而为下游智能体提供简洁、可预测的摘要
- 现有的单消息任务转移记录现在默认在 `<CONVERSATION HISTORY>` 块之前以 "For context, here is the conversation so far between the user and the previous agent:" 开头，使下游智能体获得标注清晰的回顾

### 0.5.0

此版本未引入可见的不兼容变更，但包含新功能和一些底层的重要更新：

- 为 `RealtimeRunner` 增加了处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 大幅修订了 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0

此版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请将该 SDK 与 openai v2.x 一起使用。

### 0.3.0

此版本中，Realtime API 的支持迁移至 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

此版本中，一些原先接收 `Agent` 作为参数的位置，现在改为接收 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这仅是类型层面的更改，你仍将收到 `Agent` 对象。更新时，只需将类型错误中的 `Agent` 替换为 `AgentBase` 即可。

### 0.1.0

此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增两个参数：`run_context` 和 `agent`。你需要在任何继承 `MCPServer` 的类中添加这些参数。