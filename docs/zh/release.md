---
search:
  exclude: true
---
# 发布流程/更新日志

本项目采用经轻微修改的语义化版本控制，版本号形式为 `0.Y.Z`。前导的 `0` 表示该 SDK 仍在快速演进中。各部分的递增规则如下：

## 次版本（`Y`）

对于未标记为 beta 的任何公共接口发生**不兼容变更**时，我们会提升次版本号 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 可能包含不兼容变更。

如果你不希望引入不兼容变更，建议在你的项目中固定到 `0.0.x` 版本。

## 修订版本（`Z`）

对于非破坏性变更，我们会递增 `Z`：

- Bug 修复
- 新功能
- 私有接口的变更
- 测试版功能的更新

## 不兼容变更日志

### 0.6.0

在此版本中，默认的任务转移历史现在被封装为单条 assistant 消息，而不再暴露原始的 user/assistant 轮次，从而为下游智能体提供简洁且可预测的回顾
- 现有的单消息任务转移记录默认会在 `<CONVERSATION HISTORY>` 块前以“For context, here is the conversation so far between the user and the previous agent:”开头，使下游智能体获得带有清晰标签的回顾

### 0.5.0

该版本没有引入可见的不兼容变更，但包含新特性和若干重要的底层更新：

- 为 `RealtimeRunner` 新增了处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 大幅修订了 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0

在此版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请将 openai 升级至 v2.x 并与本 SDK 一同使用。

### 0.3.0

在此版本中，Realtime API 支持迁移至 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，若干此前接收 `Agent` 作为参数的位置，现在改为接收 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这只是类型层面的变更，你仍将收到 `Agent` 对象。要更新的话，只需将类型错误中出现的 `Agent` 替换为 `AgentBase` 即可。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。你需要将这些参数添加到任何继承 `MCPServer` 的类中。