---
search:
  exclude: true
---
# 发布流程/更新日志

本项目采用略微修改的语义化版本规范，形式为 `0.Y.Z`。前导的 `0` 表示 SDK 仍在快速演进中。版本号的递增规则如下：

## 次要（`Y`）版本

对于未标记为 beta 的任何公共接口的**破坏性变更**，我们会提升次要版本号 `Y`。例如，从 `0.0.x` 升到 `0.1.x` 可能包含破坏性变更。

如果你不希望引入破坏性变更，建议在你的项目中固定使用 `0.0.x` 版本范围。

## 补丁（`Z`）版本

对于非破坏性变更，我们会提升 `Z`：

- Bug 修复
- 新功能
- 对私有接口的变更
- 对 beta 功能的更新

## 破坏性变更更新日志

### 0.4.0

在该版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请将本 SDK 与 openai v2.x 一同使用。

### 0.3.0

在该版本中，Realtime API 的支持迁移至 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在该版本中，部分原本接收 `Agent` 作为参数的位置，现改为接收 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这仅是类型层面的变更，你仍会接收到 `Agent` 对象。要更新，只需将类型错误中出现的 `Agent` 替换为 `AgentBase` 即可。

### 0.1.0

在该版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增两个参数：`run_context` 和 `agent`。你需要为任何继承自 `MCPServer` 的类添加这些参数。