---
search:
  exclude: true
---
# 发布流程/变更日志

本项目遵循一种稍作修改的语义化版本规则，采用 `0.Y.Z` 形式。前导的 `0` 表示 SDK 仍在快速演进。版本号的递增规则如下：

## 次版本（`Y`）

对于未标注为 beta 的任何公共接口出现的**破坏性变更**，我们会提升次版本号 `Y`。例如，从 `0.0.x` 升到 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在你的项目中固定到 `0.0.x` 版本。

## 修订版本（`Z`）

对于非破坏性变更，我们会提升 `Z`：

- Bug 修复
- 新功能
- 私有接口的变更
- beta 功能的更新

## 破坏性变更变更日志

### 0.6.0

在该版本中，默认的任务转移历史现在被打包为单条 assistant 消息，而不是暴露原始的 user/assistant 轮次，从而为下游智能体提供简洁、可预测的摘要
- 现有的单消息任务转移记录现在默认在 `<CONVERSATION HISTORY>` 块之前以“For context, here is the conversation so far between the user and the previous agent:”开头，使下游智能体获得标注清晰的摘要

### 0.5.0

此版本没有引入任何可见的破坏性变更，但包含新功能和一些重要的底层更新：

- 为 `RealtimeRunner` 增加了处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 为兼容 Python 3.14，显著修订了 `Runner#run_sync` 的内部逻辑

### 0.4.0

在该版本中，不再支持 [openai](https://pypi.org/project/openai/) 包的 v1.x 版本。请将 openai 升级至 v2.x 并配合本 SDK 使用。

### 0.3.0

在该版本中，Realtime API 的支持迁移到了 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在该版本中，一些过去接收 `Agent` 作为参数的地方，现在改为接收 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这纯属类型变更，你仍会接收 `Agent` 对象。要更新，只需将类型错误中的 `Agent` 替换为 `AgentBase` 即可。

### 0.1.0

在该版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增两个参数：`run_context` 和 `agent`。你需要在任何继承 `MCPServer` 的类中加入这些参数。