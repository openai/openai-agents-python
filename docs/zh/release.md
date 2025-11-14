---
search:
  exclude: true
---
# 发布流程/变更日志

本项目遵循经过轻微修改的语义化版本规范，采用 `0.Y.Z` 形式。前导的 `0` 表示该 SDK 仍在快速演进中。各部分递增规则如下：

## 次版本（`Y`）

对于未标注为 beta 的任何公共接口发生的**破坏性变更**，我们会提升次版本号 `Y`。例如，从 `0.0.x` 到 `0.1.x` 可能包含破坏性变更。

如果不希望引入破坏性变更，建议在项目中将版本固定到 `0.0.x`。

## 修订版本（`Z`）

对于非破坏性变更，我们会递增 `Z`：

- Bug 修复
- 新功能
- 私有接口的变更
- 对 beta 功能的更新

## 破坏性变更日志

### 0.5.0

该版本未引入任何可见的破坏性变更，但包含新功能以及若干底层的重要更新：

- 为 `RealtimeRunner` 增加了处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip) 的支持
- 大幅修订了 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 包的 v1.x 版本。请与本 SDK 一起使用 openai v2.x。

### 0.3.0

在此版本中，Realtime API 支持迁移至 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，若干原先接收 `Agent` 作为参数的位置，现在改为接收 `AgentBase`。例如，MCP 服务中的 `list_tools()` 调用。这只是类型方面的变更，您仍将收到 `Agent` 对象。要更新，只需将类型错误中的 `Agent` 替换为 `AgentBase` 即可。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。您需要在任何继承自 `MCPServer` 的类中加入这些参数。