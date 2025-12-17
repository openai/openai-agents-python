---
search:
  exclude: true
---
# 发布流程/更新日志

本项目遵循稍作修改的语义化版本号，形式为 `0.Y.Z`。前导的 `0` 表示该 SDK 仍在快速演进中。各部分的递增规则如下：

## 次要（`Y`）版本

对于未标注为 beta 的任何公共接口出现的**破坏性变更**，我们将提升次要版本 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在项目中固定使用 `0.0.x` 版本。

## 补丁（`Z`）版本

对于非破坏性变更，我们将递增 `Z`：

- Bug 修复
- 新功能
- 私有接口的变更
- beta 功能的更新

## 破坏性变更日志

### 0.6.0

在此版本中，默认的任务转移历史现在被打包为一条助理消息，而不是暴露原始的用户/助理轮次，从而为下游智能体提供简洁、可预测的回顾摘要
- 现有的单消息任务转移记录现在默认在 `<CONVERSATION HISTORY>` 块之前以“为了提供上下文，以下是用户与上一位智能体之间到目前为止的对话：”开头，使下游智能体获得标注清晰的回顾

### 0.5.0

该版本未引入任何可见的破坏性变更，但包含新功能及一些底层的重要更新：

- 为 `RealtimeRunner` 增加了对 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 大幅修订了 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 包的 v1.x 版本。请将 openai 升级到 v2.x 并与本 SDK 搭配使用。

### 0.3.0

在此版本中，Realtime API 的支持迁移至 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，一些过去接收 `Agent` 作为参数的地方，现在改为接收 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这只是类型层面的变更，你仍将接收 `Agent` 对象。要更新，只需将类型错误中的 `Agent` 替换为 `AgentBase`。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。你需要在所有继承 `MCPServer` 的类中添加这些参数。