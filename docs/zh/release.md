---
search:
  exclude: true
---
# 发布流程/更新日志

本项目遵循经轻微修改的语义化版本规则，使用 `0.Y.Z` 形式。前导的 `0` 表示 SDK 仍在快速演进中。各组件的递增规则如下：

## 次版本（`Y`）

对于未标记为 beta 的任何公开接口出现的**重大变更**，我们将提升次版本号 `Y`。例如，从 `0.0.x` 升到 `0.1.x` 可能包含重大变更。

如果你不希望引入重大变更，建议在你的项目中将版本固定到 `0.0.x`。

## 修订版本（`Z`）

对于非破坏性变更，我们将递增 `Z`：

- Bug 修复
- 新功能
- 私有接口的变更
- 测试版功能的更新

## 重大变更更新日志

### 0.6.0

此版本中，默认的任务转移历史现在被打包为单条助手消息，而不再暴露原始的 用户/助手 轮次，从而为下游智能体提供简洁、可预测的回顾
- 现有的单消息任务转移记录现在默认在 `<CONVERSATION HISTORY>` 区块之前以“For context, here is the conversation so far between the user and the previous agent:”开头，使下游智能体获得带有清晰标签的回顾

### 0.5.0

此版本未引入可见的重大变更，但包含新功能与若干重要的底层更新：

- 为 `RealtimeRunner` 新增对 [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 大幅修订 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0

此版本中，不再支持 [openai](https://pypi.org/project/openai/) 包的 v1.x 版本。请将 openai 升级至 v2.x 并与本 SDK 一同使用。

### 0.3.0

此版本中，Realtime API 的支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

此版本中，部分原本接收 `Agent` 作为参数的位置，现在改为接收 `AgentBase` 作为参数。例如 MCP 服务中的 `list_tools()` 调用。这仅是类型层面的变更，你仍会收到 `Agent` 对象。更新方式：将类型错误中出现的 `Agent` 替换为 `AgentBase` 即可。

### 0.1.0

此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增两个参数：`run_context` 和 `agent`。你需要在继承 `MCPServer` 的任意类中加入这些参数。