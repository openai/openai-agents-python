---
search:
  exclude: true
---
# 发布流程/更新日志

该项目采用经过轻微修改的语义化版本控制，形式为 `0.Y.Z`。前导的 `0` 表示该 SDK 仍在快速演进中。各组件按如下方式递增：

## 次版本（`Y`）

对于未标记为 beta 的任何公共接口出现**破坏性变更**时，我们将增加次版本号 `Y`。例如，从 `0.0.x` 到 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在项目中固定到 `0.0.x` 版本。

## 修订版本（`Z`）

对于非破坏性变更，我们将增加 `Z`：

- Bug 修复
- 新功能
- 私有接口的变更
- beta 功能的更新

## 破坏性变更日志

### 0.6.0

在该版本中，默认的任务转移历史现在被封装为单条助手消息，而不是暴露原始的 用户/助手 轮次，从而为下游智能体提供简洁、可预测的回顾
- 现有的单消息任务转移记录现在默认在 `<CONVERSATION HISTORY>` 块之前以“For context, here is the conversation so far between the user and the previous agent:”开头，以便下游智能体获得清晰标注的回顾

### 0.5.0

该版本未引入可见的破坏性变更，但包含新功能以及一些重要的底层更新：

- 为 `RealtimeRunner` 添加对 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 大幅修订了 `Runner#run_sync` 的内部逻辑以兼容 Python 3.14

### 0.4.0

在该版本中，不再支持 [openai](https://pypi.org/project/openai/) 包的 v1.x 版本。请将 openai 升级到 v2.x 并与本 SDK 搭配使用。

### 0.3.0

在该版本中，Realtime API 的支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在该版本中，部分原先接收 `Agent` 作为参数的位置，现在改为接收 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这纯属类型层面的更改，你仍将收到 `Agent` 对象。要更新，只需将类型错误中出现的 `Agent` 替换为 `AgentBase`。

### 0.1.0

在该版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增两个参数：`run_context` 和 `agent`。你需要在任何继承 `MCPServer` 的类中添加这些参数。