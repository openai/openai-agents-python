---
search:
  exclude: true
---
# 发布流程/变更日志

本项目采用略作修改的语义化版本规范，形式为 `0.Y.Z`。前导的 `0` 表示 SDK 仍在快速演进中。各部分的增量规则如下：

## 次版本号（`Y`）

对于未标记为 beta 的任何公共接口发生的**破坏性变更**，我们会增加次版本号 `Y`。例如，从 `0.0.x` 升至 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在你的项目中锁定到 `0.0.x` 版本。

## 修订版本号（`Z`）

对于非破坏性变更，我们会增加 `Z`：

- Bug 修复
- 新功能
- 私有接口的更改
- beta 功能的更新

## 破坏性变更日志

### 0.6.0

在此版本中，默认的任务转移历史现在被打包为单条 assistant 消息，而不再暴露原始的 用户/assistant 轮次，使下游智能体获得简洁、可预测的摘要
- 现有的单消息任务转移记录现在默认在 `<CONVERSATION HISTORY>` 块之前以“For context, here is the conversation so far between the user and the previous agent:”开头，从而让下游智能体获得一个标注清晰的回顾

### 0.5.0

此版本未引入可见的破坏性变更，但包含新功能以及若干重要的底层更新：

- 为 `RealtimeRunner` 增加了对 [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 大幅修订了 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0

在此版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请将 openai 升级到 v2.x 并配合本 SDK 使用。

### 0.3.0

在此版本中，Realtime API 的支持迁移至 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，若干原先接收 `Agent` 作为参数的地方，现在改为接收 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。此更改仅影响类型，你仍将接收 `Agent` 对象。更新方式：将类型注解中的 `Agent` 替换为 `AgentBase` 以修复类型错误。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 增加了两个参数：`run_context` 和 `agent`。你需要在任何继承自 `MCPServer` 的类中添加这些参数。