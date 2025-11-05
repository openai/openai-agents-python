---
search:
  exclude: true
---
# 发布流程/更新日志

本项目遵循稍作修改的语义化版本方案，采用 `0.Y.Z` 的形式。前导的 `0` 表示该 SDK 仍在快速演进中。版本号的递增规则如下：

## 次版本（`Y`）

对于未标注为 beta 的任何公共接口出现**破坏性变更**时，我们会提升次版本号 `Y`。例如，从 `0.0.x` 升到 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在你的项目中固定依赖到 `0.0.x` 版本。

## 补丁版本（`Z`）

对于非破坏性变更，我们会递增 `Z`：

- Bug 修复
- 新功能
- 私有接口的变更
- beta 特性的更新

## 重大变更更新日志

### 0.4.0

在该版本中，不再支持 [openai](https://pypi.org/project/openai/) 包的 v1.x 版本。请配合本 SDK 使用 openai v2.x。

### 0.3.0

在该版本中，Realtime API 的支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在该版本中，部分原本接收 `Agent` 作为参数的地方，现在改为接收 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这是一次纯类型变更，你仍然会收到 `Agent` 对象。要更新的话，只需将类型错误中的 `Agent` 替换为 `AgentBase`。

### 0.1.0

在该版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。你需要在任何继承自 `MCPServer` 的类中添加这些参数。