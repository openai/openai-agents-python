---
search:
  exclude: true
---
# 发布流程/变更日志

本项目遵循略作修改的语义化版本控制，采用 `0.Y.Z` 形式。开头的 `0` 表示该 SDK 仍在快速演进。各组成部分按如下方式递增：

## Minor（`Y`）版本

对于任何未标记为 beta 的公共接口中的**破坏性变更**，我们会递增 minor 版本 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 可能包含破坏性变更。

如果你不希望遇到破坏性变更，建议在项目中固定使用 `0.0.x` 版本。

## Patch（`Z`）版本

对于非破坏性变更，我们会递增 `Z`：

-   Bug 修复
-   新功能
-   私有接口变更
-   beta 功能更新

## 破坏性变更日志

### 0.15.0

在此版本中，模型拒绝现在会明确呈现为 `ModelRefusalError`，而不是被视为空文本输出；对于 structured outputs，也不再导致运行循环持续重试直到 `MaxTurnsExceeded`。

这会影响此前预期仅包含拒绝的模型响应会以 `final_output == ""` 完成的代码。若要在不抛出异常的情况下处理拒绝，请提供 `model_refusal` 运行错误处理器：

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

对于 structured outputs 智能体，该处理器可以返回一个与智能体输出 schema 匹配的值，SDK 会像验证其他运行错误处理器最终输出一样对其进行验证。

### 0.14.0

此 minor 版本**不会**引入破坏性变更，但新增了一个重要的 beta 功能领域：Sandbox Agents，以及在本地、容器化和托管环境中使用它们所需的运行时、后端和文档支持。

亮点：

-   新增了以 `SandboxAgent`、`Manifest` 和 `SandboxRunConfig` 为中心的 beta 沙箱运行时界面，使智能体能够在持久化的隔离工作区中处理文件、目录、Git 仓库、挂载、快照和恢复支持。
-   通过 `UnixLocalSandboxClient` 和 `DockerSandboxClient` 为本地和容器化开发新增了沙箱执行后端，并通过可选 extras 集成了 Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop 和 Vercel 等托管提供方。
-   新增沙箱记忆支持，使未来运行可以复用先前运行中的经验，并支持渐进式披露、多轮分组、可配置的隔离边界，以及包括基于 S3 的工作流在内的持久化记忆示例。
-   新增更广泛的工作区和恢复模型，包括本地与合成工作区条目、用于 S3/R2/GCS/Azure Blob Storage/S3 Files 的远程存储挂载、可移植快照，以及通过 `RunState`、`SandboxSessionState` 或已保存快照实现的恢复流程。
-   在 `examples/sandbox/` 下新增了大量沙箱示例和教程，涵盖使用技能、任务转移、记忆、特定提供方设置的编码任务，以及代码审查、dataroom QA 和网站克隆等端到端工作流。
-   扩展了核心运行时和追踪栈，新增了感知沙箱的会话准备、能力绑定、状态序列化、统一追踪、提示缓存键默认值，以及更安全的敏感 MCP 输出脱敏。

### 0.13.0

此 minor 版本**不会**引入破坏性变更，但包含一个值得注意的 Realtime 默认值更新，以及新的 MCP 能力和运行时稳定性修复。

亮点：

-   默认的 websocket Realtime 模型现在是 `gpt-realtime-1.5`，因此新的 Realtime 智能体设置无需额外配置即可使用较新的模型。
-   `MCPServer` 现在公开 `list_resources()`、`list_resource_templates()` 和 `read_resource()`，并且 `MCPServerStreamableHttp` 现在公开 `session_id`，因此 streamable HTTP 会话可以在重新连接或无状态 worker 之间恢复。
-   Chat Completions 集成现在可以通过 `should_replay_reasoning_content` 选择启用 reasoning-content 回放，从而改善 LiteLLM/DeepSeek 等适配器的特定提供方推理/工具调用连续性。
-   修复了若干运行时和会话边界情况，包括 `SQLAlchemySession` 中的并发首次写入、推理剥离后带有孤立 assistant 消息 ID 的压缩请求、`remove_all_tools()` 遗留 MCP/推理项，以及工具调用批量执行器中的竞态问题。

### 0.12.0

此 minor 版本**不会**引入破坏性变更。有关主要功能新增内容，请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)。

### 0.11.0

此 minor 版本**不会**引入破坏性变更。有关主要功能新增内容，请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)。

### 0.10.0

此 minor 版本**不会**引入破坏性变更，但为 OpenAI Responses 用户包含了一个重要的新功能领域：Responses API 的 websocket 传输支持。

亮点：

-   为 OpenAI Responses 模型新增 websocket 传输支持（可选启用；HTTP 仍为默认传输）。
-   新增 `responses_websocket_session()` 辅助函数 / `ResponsesWebSocketSession`，用于在多轮运行中复用共享的支持 websocket 的提供方和 `RunConfig`。
-   新增 websocket 流式传输示例（`examples/basic/stream_ws.py`），涵盖流式传输、tools、审批和后续轮次。

### 0.9.0

在此版本中，Python 3.9 不再受支持，因为该主版本已在三个月前达到 EOL。请升级到更新的运行时版本。

此外，`Agent#as_tool()` 方法返回值的类型提示已从 `Tool` 收窄为 `FunctionTool`。此变更通常不会导致破坏性问题，但如果你的代码依赖更宽泛的联合类型，可能需要在你这边做一些调整。

### 0.8.0

在此版本中，两项运行时行为变更可能需要迁移工作：

- 包装**同步** Python 可调用对象的工具调用现在会通过 `asyncio.to_thread(...)` 在 worker 线程上执行，而不是在事件循环线程上运行。如果你的工具逻辑依赖线程本地状态或线程亲和资源，请迁移到异步工具实现，或在工具代码中显式处理线程亲和性。
- 本地 MCP 工具失败处理现在可配置，默认行为可以返回模型可见的错误输出，而不是让整个运行失败。如果你依赖快速失败语义，请设置 `mcp_config={"failure_error_function": None}`。服务级别的 `failure_error_function` 值会覆盖智能体级别设置，因此请在每个具有显式处理器的本地 MCP 服务上设置 `failure_error_function=None`。

### 0.7.0

在此版本中，有几项行为变更可能影响现有应用：

- 嵌套任务转移历史现在为**可选启用**（默认禁用）。如果你依赖 v0.6.x 默认的嵌套行为，请显式设置 `RunConfig(nest_handoff_history=True)`。
- `gpt-5.1` / `gpt-5.2` 的默认 `reasoning.effort` 已从 SDK 默认配置的先前默认值 `"low"` 更改为 `"none"`。如果你的提示或质量/成本配置依赖 `"low"`，请在 `model_settings` 中显式设置。

### 0.6.0

在此版本中，默认任务转移历史现在会被打包到一条 assistant 消息中，而不是暴露原始的用户/assistant 轮次，从而为下游智能体提供简洁、可预测的回顾
- 现有的单消息任务转移转录现在默认会在 `<CONVERSATION HISTORY>` 块之前以 “For context, here is the conversation so far between the user and the previous agent:” 开头，因此下游智能体会获得带有清晰标签的回顾

### 0.5.0

此版本没有引入任何可见的破坏性变更，但包含了新功能以及若干重要的底层更新：

- 新增对 `RealtimeRunner` 处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip)的支持
- 为兼容 Python 3.14，显著修订了 `Runner#run_sync` 的内部逻辑

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 包 v1.x 版本。请将 openai v2.x 与此 SDK 一起使用。

### 0.3.0

在此版本中，Realtime API 支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，过去使用 `Agent` 作为参数的一些地方，现在改为使用 `AgentBase` 作为参数。例如，MCP 服务中的 `list_tools()` 调用。这是纯类型层面的变更，你仍会收到 `Agent` 对象。要更新，只需通过将 `Agent` 替换为 `AgentBase` 来修复类型错误。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 有两个新参数：`run_context` 和 `agent`。你需要将这些参数添加到任何继承 `MCPServer` 的类中。