---
search:
  exclude: true
---
# 发布流程/更新日志

本项目采用经过轻微修改的语义化版本，格式为 `0.Y.Z`。前导的 `0` 表示该 SDK 仍在快速演进中。版本号的递增规则如下：

## 次要 (`Y`) 版本

对于未标记为 beta 的任何公共接口发生的**破坏性变更**，我们将提升次要版本号 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在项目中固定到 `0.0.x` 版本。

## 修订（`Z`）版本

对于非破坏性变更，我们将递增 `Z`：

- Bug 修复
- 新功能
- 私有接口的变更
- beta 功能的更新

## 破坏性变更日志

### 0.6.0

此版本中，默认的任务转移历史现在被打包为单条助手消息，而不是暴露原始的 用户/助手 轮次，为下游智能体提供简洁且可预测的回顾
- 现有的单消息任务转移记录默认以 "For context, here is the conversation so far between the user and the previous agent:" 开头，随后是 `<CONVERSATION HISTORY>` 块，从而让下游智能体获得清晰标注的回顾

### 0.5.0

此版本未引入任何可见的破坏性变更，但包含新功能与多项底层重要更新：

- 为 `RealtimeRunner` 增加了对 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 为兼容 Python 3.14，显著修订了 `Runner#run_sync` 的内部逻辑

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 包的 v1.x 版本。请配合本 SDK 使用 openai v2.x。

### 0.3.0

在此版本中，Realtime API 支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，部分原本接收 `Agent` 作为参数的位置，现在改为接收 `AgentBase` 作为参数。例如 MCP 服务 中的 `list_tools()` 调用。这仅是类型层面的变更，你仍会接收到 `Agent` 对象。更新方式：将类型错误中的 `Agent` 替换为 `AgentBase` 即可。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。你需要在继承 `MCPServer` 的任意类中添加这些参数。