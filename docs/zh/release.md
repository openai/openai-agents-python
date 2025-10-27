---
search:
  exclude: true
---
# 发布流程/更新日志

本项目采用略经修改的语义化版本控制，形式为 `0.Y.Z`。前导的 `0` 表示该 SDK 仍在快速演进中。各部分递增规则如下：

## 次要（`Y`）版本

对于未标记为 beta 的任何公共接口发生**重大变更**时，我们会提升次要版本 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 可能包含重大变更。

如果你不希望引入重大变更，我们建议在你的项目中固定到 `0.0.x` 版本。

## 修订（`Z`）版本

对于非破坏性变更，我们会递增 `Z`：

- Bug 修复
- 新功能
- 对私有接口的更改
- 对 beta 功能的更新

## 重大变更更新日志

### 0.4.0

在该版本中，[openai](https://pypi.org/project/openai/) 包的 v1.x 版本不再受支持。请将 openai 升级到 v2.x 并与本 SDK 一起使用。

### 0.3.0

在该版本中，Realtime API 的支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在该版本中，原本接受 `Agent` 作为参数的若干位置，现在改为接受 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这只是类型方面的更改，你仍将收到 `Agent` 对象。要更新的话，只需将类型中的 `Agent` 替换为 `AgentBase` 来修复类型错误。

### 0.1.0

在该版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。你需要在任何继承自 `MCPServer` 的类中添加这些参数。