---
search:
  exclude: true
---
# 发布流程/更新日志

本项目遵循稍作修改的语义化版本控制，采用 `0.Y.Z` 的形式。前导的 `0` 表示该 SDK 仍在快速演进中。各部分按如下规则递增：

## 次版本（`Y`）

对于任何未标记为测试版的公开接口的**破坏性变更**，我们将提升次版本号 `Y`。例如，从 `0.0.x` 到 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在你的项目中固定到 `0.0.x` 版本。

## 修订版本（`Z`）

对于非破坏性变更，我们将递增 `Z`：

- Bug 修复
- 新功能
- 私有接口的更改
- 测试版功能的更新

## 破坏性变更更新日志

### 0.4.0

在该版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请与本 SDK 一起使用 openai v2.x。

### 0.3.0

在该版本中，Realtime API 的支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在该版本中，一些原先接收 `Agent` 作为参数的地方现在改为接收 `AgentBase`。例如，MCP 服务中的 `list_tools()` 调用。这仅是类型层面的变更，你仍会收到 `Agent` 对象。更新时，只需将类型错误中出现的 `Agent` 替换为 `AgentBase` 即可。

### 0.1.0

在该版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。你需要在任何继承 `MCPServer` 的类中添加这些参数。