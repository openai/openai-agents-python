---
search:
  exclude: true
---
# 发布流程/变更日志

本项目采用略作修改的语义化版本控制，版本格式为 `0.Y.Z`。开头的 `0` 表示 SDK 仍在快速演进。各组成部分按以下规则递增：

## 次版本（`Y`）

对于任何未标记为 beta 的公共接口，如果发生**破坏性变更**，我们将递增次版本 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 时可能包含破坏性变更。

如果不希望引入破坏性变更，建议在项目中将版本锁定为 `0.0.x`。

## 修订版本（`Z`）

对于非破坏性变更，我们将递增 `Z`：

- Bug 修复
- 新功能
- 私有接口变更
- beta 功能更新

## 破坏性变更日志

### 0.19.0

此次次版本发布**没有**引入破坏性变更。次版本号的提升反映了 OpenAI Responses 的一个重要新功能领域：程序化工具调用。

亮点：

- 新增 [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool]，允许受支持的 OpenAI Responses 模型生成 JavaScript，以协调符合条件的工具。它支持按工具配置 `allowed_callers`、结构化工具调用输出，并可与 Runner 流式传输、安全防护措施、审批、会话和 `RunState` 集成。有关设置方式和限制，请参阅[程序化工具调用](tools.md#programmatic-tool-calling)。
- 新增公共 `agents.decorators` 模块，并在现有工具调用和安全防护措施装饰器之外，增加了更简短的 `@tool` 别名。工具调用现在也支持异步可调用对象。
- SDK 配置现在统一支持在智能体、运行、模型、会话、沙箱和语音流水线中使用类型化设置对象或字典，并会验证未知设置。
- 加强了模型、工具、MCP、Realtime、会话、沙箱和追踪中的错误及诊断日志记录，以避免暴露原始敏感载荷，同时保留有用的调试上下文。
- 改进了 AnyLLM、LiteLLM 和 Chat Completions兼容性，在模型重试期间保留会话历史，并针对响应开始前发生的 WebSocket 过载新增了提供商重试指引，以便在允许重放时，由选择启用的 Runner 重试策略进行处理。
- 通过 `VercelCloudBucketMountStrategy` 新增了[仅可在创建时使用的 Vercel 沙箱 S3 挂载](sandbox/clients.md#mounts-and-remote-storage)。挂载了存储桶的会话不会将存储桶内容纳入工作区持久化，并且有意不支持动态更改挂载或恢复会话。

### 0.18.0

此次次版本发布**没有**引入破坏性变更。次版本号的提升仅用于 Realtime智能体默认模型更新。

亮点：

- Realtime智能体现在默认使用 `gpt-realtime-2.1` 模型，因此新的 Realtime 设置无需额外配置即可使用最新的推荐模型。

### 0.17.0

在此版本中，沙箱本地源物化会将 `LocalFile.src` 和 `LocalDir.src` 限制在物化 `base_dir` 内，除非源路径已包含在 `Manifest.extra_path_grants` 中。应用清单时，`base_dir` 是 SDK 进程的当前工作目录；相对本地源路径从该目录解析，而绝对本地源路径必须已位于该目录内或某个明确授权的目录下。此变更修复了本地工件边界问题，但可能会影响有意将该基础目录以外的可信主机文件或目录复制到沙箱工作区的应用。

迁移时，请使用 `SandboxPathGrant` 在清单级别授权可信主机根目录；如果沙箱只需读取这些文件，最好将其设置为只读：

```python
from pathlib import Path

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.entries import Dir, LocalDir

# This is an absolute host path outside the SDK process base_dir.
TRUSTED_DOCS_ROOT = Path("/opt/my-app/docs")

manifest = Manifest(
    extra_path_grants=(
        # This host root is outside the SDK process base_dir, so the manifest must grant it.
        SandboxPathGrant(path=str(TRUSTED_DOCS_ROOT), read_only=True),
    ),
    entries={
        # No grant is needed for local sources that stay under the SDK process base_dir.
        "fixtures": LocalDir(src=Path("fixtures"), description="Local test fixtures."),
        # This entry reads from the granted host root and copies it into the sandbox workspace.
        "docs": LocalDir(src=TRUSTED_DOCS_ROOT, description="Trusted local documents."),
        # Dir creates a sandbox workspace directory; it does not read from the host filesystem.
        "output": Dir(description="Generated artifacts."),
    },
)
```

请将 `extra_path_grants` 视为可信的应用配置。除非应用已批准相应主机路径，否则不要根据模型输出或其他不可信的清单输入填充授权。

### 0.16.0

在此版本中，SDK 默认模型由 `gpt-4.1` 更改为 `gpt-5.4-mini`。这会影响未显式设置模型的智能体和运行。由于新的默认模型是 GPT-5 模型，隐式默认模型设置现在包含 GPT-5 的默认值，例如 `reasoning.effort="none"` 和 `verbosity="low"`。

如果需要保留之前的默认模型行为，请在智能体或运行配置中显式设置模型，或者设置 `OPENAI_DEFAULT_MODEL` 环境变量：

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

亮点：

- `Runner.run`、`Runner.run_sync` 和 `Runner.run_streamed` 现在接受 `max_turns=None`，以禁用轮次限制。
- 对于本地、Docker 和由提供商支持的沙箱实现，沙箱工作区初始化现在会拒绝包含指向归档根目录之外的符号链接的 tar 归档，包括目标为绝对路径的符号链接。

### 0.15.0

在此版本中，模型拒绝现在会显式呈现为 `ModelRefusalError`，而不再被视为空文本输出；对于structured outputs，也不会再导致运行循环持续重试，直至触发 `MaxTurnsExceeded`。

这会影响之前预期仅包含拒绝的模型响应以 `final_output == ""` 完成的代码。如需在不抛出异常的情况下处理拒绝，请提供 `model_refusal` 运行错误处理程序：

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

对于使用structured outputs的智能体，处理程序可以返回符合智能体输出架构的值，SDK 将像验证其他运行错误处理程序的最终输出一样对其进行验证。

### 0.14.0

此次次版本发布**没有**引入破坏性变更，但新增了一个重要的 beta 功能领域：沙箱智能体，以及在本地、容器化和托管环境中使用它们所需的运行时、后端和文档支持。

亮点：

- 新增以 `SandboxAgent`、`Manifest` 和 `SandboxRunConfig` 为核心的 beta 沙箱运行时接口，使智能体能够在持久化的隔离工作区内处理文件、目录、Git 仓库、挂载和快照，并支持恢复。
- 新增通过 `UnixLocalSandboxClient` 和 `DockerSandboxClient` 实现的本地及容器化开发沙箱执行后端，并通过可选扩展提供 Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop 和 Vercel 的托管提供商集成。
- 新增沙箱记忆支持，使后续运行可以复用此前运行中的经验，并支持渐进式披露、多轮分组、可配置的隔离边界，以及包括 S3 支持工作流在内的持久化记忆代码示例。
- 新增更广泛的工作区和恢复模型，包括本地及合成工作区条目、S3/R2/GCS/Azure Blob Storage/S3 Files 远程存储挂载、可移植快照，以及通过 `RunState`、`SandboxSessionState` 或已保存快照实现的恢复流程。
- 在 `examples/sandbox/` 下新增大量沙箱代码示例和教程，涵盖使用技能完成编码任务、任务转移、记忆、提供商特定设置，以及代码审查、数据室问答和网站克隆等端到端工作流。
- 扩展了核心运行时和追踪技术栈，新增沙箱感知的会话准备、能力绑定、状态序列化、统一追踪、提示缓存键默认值，以及更安全的敏感MCP输出遮蔽。

### 0.13.0

此次次版本发布**没有**引入破坏性变更，但包含一项值得注意的 Realtime 默认设置更新，以及新的MCP能力和运行时稳定性修复。

亮点：

- 默认 WebSocket Realtime 模型现在是 `gpt-realtime-1.5`，因此新的 Realtime智能体设置无需额外配置即可使用较新的模型。
- `MCPServer` 现在公开 `list_resources()`、`list_resource_templates()` 和 `read_resource()`，而 `MCPServerStreamableHttp` 现在公开 `session_id`，因此可流式 HTTP 会话可在重新连接后或无状态工作进程之间恢复。
- Chat Completions集成现在可以通过 `should_replay_reasoning_content` 选择启用推理内容重放，从而改善 LiteLLM/DeepSeek 等适配器中特定于提供商的推理及工具调用连续性。
- 修复了多项运行时和会话边界情况，包括 `SQLAlchemySession` 中并发首次写入、移除推理内容后压缩请求包含孤立的助手消息 ID、`remove_all_tools()` 遗留MCP/推理项，以及工具调用批处理执行器中的竞态问题。

### 0.12.0

此次次版本发布**没有**引入破坏性变更。有关主要新增功能，请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)。

### 0.11.0

此次次版本发布**没有**引入破坏性变更。有关主要新增功能，请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)。

### 0.10.0

此次次版本发布**没有**引入破坏性变更，但为 OpenAI Responses用户新增了一个重要功能领域：Responses API 的 WebSocket 传输支持。

亮点：

- 新增 OpenAI Responses模型的 WebSocket 传输支持（需选择启用；HTTP 仍是默认传输方式）。
- 新增 `responses_websocket_session()` 辅助函数/`ResponsesWebSocketSession`，用于在多轮运行之间复用支持 WebSocket 的共享提供商和 `RunConfig`。
- 新增 WebSocket 流式传输代码示例（`examples/basic/stream_ws.py`），涵盖流式传输、工具、审批和后续轮次。

### 0.9.0

在此版本中，不再支持 Python 3.9，因为该主要版本已于三个月前结束生命周期（EOL）。请升级到较新的运行时版本。

此外，`Agent#as_tool()` 方法返回值的类型提示已从 `Tool` 缩窄为 `FunctionTool`。此变更通常不会导致破坏性问题，但如果代码依赖更宽泛的联合类型，则可能需要进行相应调整。

### 0.8.0

在此版本中，两项运行时行为变更可能需要执行迁移：

- 封装**同步** Python 可调用对象的工具调用现在通过 `asyncio.to_thread(...)` 在工作线程上执行，而不再在事件循环线程上运行。如果工具逻辑依赖线程局部状态或具有线程亲和性的资源，请迁移到异步工具实现，或在工具代码中显式指定线程亲和性。
- 本地MCP工具的失败处理现在可配置，并且默认行为可以返回模型可见的错误输出，而不是使整个运行失败。如果依赖快速失败语义，请设置 `mcp_config={"failure_error_function": None}`。服务级 `failure_error_function` 值会覆盖智能体级设置，因此请在每个具有显式处理程序的本地MCP服务上设置 `failure_error_function=None`。

### 0.7.0

在此版本中，有几项行为变更可能会影响现有应用：

- 嵌套任务转移历史现在需要**选择启用**（默认禁用）。如果依赖 v0.6.x 默认的嵌套行为，请显式设置 `RunConfig(nest_handoff_history=True)`。
- `gpt-5.1`/`gpt-5.2` 的默认 `reasoning.effort` 已从 SDK 默认值所配置的 `"low"` 更改为 `"none"`。如果提示词或质量/成本配置依赖 `"low"`，请在 `model_settings` 中显式设置。

### 0.6.0

在此版本中，默认任务转移历史现在会封装为一条助手消息，而不再公开原始用户/助手轮次，从而为下游智能体提供简洁、可预测的回顾
- 现有的单消息任务转移对话记录现在默认会在 `<CONVERSATION HISTORY>` 块之前以“For context, here is the conversation so far between the user and the previous agent:”开头，从而为下游智能体提供带有清晰标签的回顾

### 0.5.0

此版本未引入任何可见的破坏性变更，但新增了功能，并对内部实现进行了几项重要更新：

- 新增 `RealtimeRunner` 对处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip)的支持
- 大幅调整了 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 软件包的 v1.x 版本。请将 openai v2.x 与此 SDK 配合使用。

### 0.3.0

在此版本中，Realtime API支持迁移至 gpt-realtime 模型及其 API 接口（正式发布版本）。

### 0.2.0

在此版本中，少数原本将 `Agent` 作为参数的位置现在改为使用 `AgentBase`。例如，MCP服务中的 `list_tools()` 调用。这纯粹是类型层面的变更，实际仍会收到 `Agent` 对象。更新时，只需将 `Agent` 替换为 `AgentBase` 以修复类型错误。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。任何继承 `MCPServer` 的类都需要添加这些参数。