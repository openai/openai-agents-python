---
search:
  exclude: true
---
# 发布流程/变更日志

本项目采用略作修改的语义化版本控制，形式为 `0.Y.Z`。前导的 `0` 表示该 SDK 仍在快速演进中。各组件的递增规则如下：

## 次要（`Y`）版本

对于未标记为 beta 的任何公共接口出现**破坏性变更**时，我们会提升次要版本 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在你的项目中固定到 `0.0.x` 版本。

## 补丁（`Z`）版本

对于非破坏性变更，我们会递增 `Z`：

- Bug 修复
- 新增特性
- 对私有接口的更改
- 对 beta 特性的更新

## 破坏性变更日志

### 0.6.0

在该版本中，默认的任务转移历史现被打包为单条 assistant 消息，而不是暴露原始的用户/assistant 轮次，从而为下游智能体提供简洁、可预测的回顾
- 现有的单消息任务转移记录默认以 "For context, here is the conversation so far between the user and the previous agent:" 开始，位于 `<CONVERSATION HISTORY>` 块之前，以便下游智能体获得清晰标注的回顾

### 0.5.0

此版本没有引入可见的破坏性变更，但包含新特性以及若干重要的底层更新：

- 为 `RealtimeRunner` 新增了处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 大幅修订了 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0

在该版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请将 openai 升级到 v2.x 并与本 SDK 搭配使用。

### 0.3.0

在该版本中，Realtime API 的支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在该版本中，若干原本接收 `Agent` 作为参数的地方，现在改为接收 `AgentBase` 作为参数。例如 MCP 服务中的 `list_tools()` 调用。此更改仅影响类型，你仍将收到 `Agent` 对象。要更新，只需将类型错误中的 `Agent` 替换为 `AgentBase`。

### 0.1.0

在该版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。你需要为继承 `MCPServer` 的任何类添加这些参数。