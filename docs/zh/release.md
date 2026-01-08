---
search:
  exclude: true
---
# 发布流程/变更日志

本项目采用经轻微修改的语义化版本，格式为 `0.Y.Z`。前导的 `0` 表示该 SDK 仍在快速演进。各部分递增规则如下：

## 次版本（`Y`）

对于未标记为 beta 的任何公共接口发生的**破坏性变更**，我们将提升次版本号 `Y`。例如，从 `0.0.x` 升至 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在项目中固定到 `0.0.x` 版本。

## 修订版本（`Z`）

对于非破坏性变更，我们将递增 `Z`：

- Bug 修复
- 新功能
- 对私有接口的更改
- 对 beta 功能的更新

## 破坏性变更日志

### 0.6.0

在该版本中，默认的任务转移历史现在被打包为单条助手消息，而不再暴露原始的用户/助手轮次，为下游智能体提供简洁、可预测的回顾
- 现有的单条消息任务转移对话记录默认在 `<CONVERSATION HISTORY>` 区块前以“For context, here is the conversation so far between the user and the previous agent:”开头，使下游智能体获得清晰标注的回顾

### 0.5.0

此版本未引入可见的破坏性变更，但包含新功能以及一些重要的底层更新：

- 为 `RealtimeRunner` 增加了对 [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 大幅修订了 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0

在该版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请配合本 SDK 使用 openai v2.x。

### 0.3.0

在该版本中，Realtime API 的支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在该版本中，一些原先接收 `Agent` 作为参数的地方，现在改为接收 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这纯属类型变更，你依然会收到 `Agent` 对象。要更新，请将类型错误中的 `Agent` 替换为 `AgentBase`。

### 0.1.0

在该版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。你需要将这些参数添加到任何继承自 `MCPServer` 的类中。