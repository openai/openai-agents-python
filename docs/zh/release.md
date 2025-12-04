---
search:
  exclude: true
---
# 发布流程/变更日志

本项目遵循略作修改的语义化版本规范，采用 `0.Y.Z` 的形式。前导的 `0` 表示该 SDK 仍在快速演进中。版本号的递增规则如下：

## 次版本（`Y`）

对于任何未标注为 beta 的公共接口的**不兼容变更**，我们会提升次版本号 `Y`。例如，从 `0.0.x` 升至 `0.1.x` 可能包含不兼容变更。

如果你不希望引入不兼容变更，建议在你的项目中固定到 `0.0.x` 版本。

## 修订版本（`Z`）

对于不引入不兼容变更的更新，我们会提升 `Z`：

- 错误修复
- 新功能
- 私有接口的变更
- beta 功能的更新

## 重大变更日志

### 0.6.0

在该版本中，默认的任务转移历史现在被打包为单条助手消息，而不再暴露原始的 用户/助手 轮次，为下游智能体提供简洁、可预测的摘要
- 现有的单消息任务转移记录默认以 "For context, here is the conversation so far between the user and the previous agent:" 开头，随后是 `<CONVERSATION HISTORY>` 块，以便下游智能体获得带有清晰标签的回顾

### 0.5.0

此版本没有引入可见的不兼容变更，但包含新功能以及若干重要的底层更新：

- 为 `RealtimeRunner` 增加了对 [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 大幅修订了 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0

在该版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请配合本 SDK 使用 openai v2.x。

### 0.3.0

在该版本中，Realtime API 支持迁移到了 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在该版本中，部分原本接收 `Agent` 作为参数的地方，现在改为接收 `AgentBase`。例如，MCP 服务中的 `list_tools()` 调用。这仅是类型层面的变更，你仍将收到 `Agent` 对象。要更新，只需将类型错误修复为将 `Agent` 替换为 `AgentBase`。

### 0.1.0

在该版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。你需要为所有继承 `MCPServer` 的类添加这些参数。