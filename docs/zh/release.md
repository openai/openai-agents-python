---
search:
  exclude: true
---
# 发布流程/变更日志

本项目采用略作修改的语义化版本控制，格式为 `0.Y.Z`。开头的 `0` 表示 SDK 仍在快速演进。各部分按以下方式递增：

## 次版本（`Y`）

对于未标记为 beta 的任何公共接口所发生的**破坏性变更**，我们将递增次版本号 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 时可能包含破坏性变更。

如果您不希望遇到破坏性变更，建议在项目中将版本固定为 `0.0.x`。

## 补丁版本（`Z`）

对于非破坏性变更，我们将递增 `Z`：

-   Bug 修复
-   新功能
-   私有接口变更
-   beta 功能更新

## 破坏性变更日志

### 0.19.0

此次次版本发布**不**引入破坏性变更。次版本号的提升反映了 OpenAI Responses 的一个重要新功能领域：程序化工具调用。

亮点：

-   新增 [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool]，支持的 OpenAI Responses 模型可借此生成 JavaScript 来协调符合条件的工具。它支持针对每个工具的 `allowed_callers`、结构化函数工具输出，并与 Runner 流式传输、安全防护措施、审批、会话及 `RunState` 集成。有关设置和限制，请参阅[程序化工具调用](tools.md#programmatic-tool-calling)。
-   新增公共 `agents.decorators` 模块和更简洁的 `@tool` 别名，同时保留现有的函数及安全防护措施装饰器。函数工具现在还支持异步可调用对象。
-   智能体、运行、模型、会话、沙箱和语音管线的 SDK 配置现在都能以一致的方式接受类型化设置对象或字典，并验证未知设置。
-   加强了模型、工具、MCP、Realtime、会话、沙箱和追踪中的错误及诊断日志，在保留有用调试上下文的同时，避免暴露原始敏感载荷。
-   改进了 AnyLLM、LiteLLM 和 Chat Completions 兼容性，在模型重试时保留会话历史，并对响应开始前发生的 WebSocket 过载错误进行重试。
-   新增了通过 `VercelCloudBucketMountStrategy` 实现的[Vercel 沙箱创建时专用 S3 挂载](sandbox/clients.md#mounts-and-remote-storage)。包含挂载的会话会在工作区持久化时排除存储桶内容，并且有意不支持动态挂载更改或会话恢复。

### 0.18.0

此次次版本发布**不**引入破坏性变更。次版本号的提升仅用于 Realtime 智能体默认模型更新。

亮点：

-   Realtime 智能体现在默认使用 `gpt-realtime-2.1`，因此新的 Realtime 配置无须额外设置即可使用最新的推荐模型。

### 0.17.0

在此版本中，沙箱本地源材料化会将 `LocalFile.src` 和 `LocalDir.src` 限制在材料化 `base_dir` 内，除非源路径已包含在 `Manifest.extra_path_grants` 中。应用清单时，`base_dir` 是 SDK 进程的当前工作目录；相对本地源路径从该目录解析，而绝对本地源路径必须已位于该目录内或显式授权的路径下。此变更修复了本地产物边界问题，但可能会影响有意将该基础目录之外的可信主机文件或目录复制到沙箱工作区的应用。

如需迁移，请使用 `SandboxPathGrant` 在清单级别授权可信主机根目录；如果沙箱只需读取这些文件，最好将授权设为只读：

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

请将 `extra_path_grants` 视为可信的应用配置。除非您的应用已批准相关主机路径，否则不要使用模型输出或其他不可信的清单输入来填充授权。

### 0.16.0

在此版本中，SDK 默认模型已从 `gpt-4.1` 更改为 `gpt-5.4-mini`。这会影响未显式设置模型的智能体和运行。由于新的默认模型是 GPT-5 模型，隐式默认模型设置现在包括 GPT-5 的默认值，例如 `reasoning.effort="none"` 和 `verbosity="low"`。

如果需要保留此前的默认模型行为，请在智能体或运行配置中显式设置模型，或设置 `OPENAI_DEFAULT_MODEL` 环境变量：

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

亮点：

-   `Runner.run`、`Runner.run_sync` 和 `Runner.run_streamed` 现在接受 `max_turns=None`，以禁用轮次限制。
-   在本地、Docker 和提供商支持的沙箱实现中，沙箱工作区数据填充现在会拒绝包含指向归档根目录之外的符号链接的 tar 归档，包括目标为绝对路径的符号链接。

### 0.15.0

在此版本中，模型拒绝现在会显式呈现为 `ModelRefusalError`，而不再被视为空文本输出；对于 structured outputs，也不再导致运行循环不断重试直至触发 `MaxTurnsExceeded`。

这会影响此前预期仅含拒绝的模型响应以 `final_output == ""` 完成的代码。若要处理拒绝而不引发异常，请提供 `model_refusal` 运行错误处理程序：

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

对于使用 structured outputs 的智能体，处理程序可以返回与智能体输出 schema 匹配的值，SDK 会像验证其他运行错误处理程序的最终输出一样对其进行验证。

### 0.14.0

此次次版本发布**不**引入破坏性变更，但新增了一个重要的 beta 功能领域：沙箱智能体，以及在本地、容器化和托管环境中使用该功能所需的运行时、后端和文档支持。

亮点：

-   新增以 `SandboxAgent`、`Manifest` 和 `SandboxRunConfig` 为核心的 beta 沙箱运行时接口，使智能体能够在持久化隔离工作区中处理文件、目录、Git 仓库、挂载、快照，并支持恢复。
-   通过 `UnixLocalSandboxClient` 和 `DockerSandboxClient` 新增用于本地和容器化开发的沙箱执行后端，并通过可选附加依赖提供 Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop 和 Vercel 的托管提供商集成。
-   新增沙箱记忆支持，使未来运行可以复用先前运行中的经验，并提供渐进式披露、多轮分组、可配置的隔离边界，以及包含 S3 支持工作流的持久化记忆代码示例。
-   新增更全面的工作区和恢复模型，包括本地与合成工作区条目、S3/R2/GCS/Azure Blob Storage/S3 Files 的远程存储挂载、可移植快照，以及通过 `RunState`、`SandboxSessionState` 或已保存快照执行的恢复流程。
-   在 `examples/sandbox/` 下新增大量沙箱代码示例和教程，涵盖使用技能的编码任务、任务转移、记忆、特定提供商配置，以及代码审查、数据室问答和网站克隆等端到端工作流。
-   扩展核心运行时和追踪栈，加入可感知沙箱的会话准备、能力绑定、状态序列化、统一追踪、提示词缓存键默认值，以及更安全的敏感 MCP 输出脱敏。

### 0.13.0

此次次版本发布**不**引入破坏性变更，但包含一项值得注意的 Realtime 默认设置更新，以及新的 MCP 功能和运行时稳定性修复。

亮点：

-   默认的 websocket Realtime 模型现在是 `gpt-realtime-1.5`，因此新的 Realtime 智能体配置无须额外设置即可使用更新的模型。
-   `MCPServer` 现在公开 `list_resources()`、`list_resource_templates()` 和 `read_resource()`，而 `MCPServerStreamableHttp` 现在公开 `session_id`，以便可流式传输的 HTTP 会话在重新连接后或无状态工作进程之间恢复。
-   Chat Completions 集成现在可以通过 `should_replay_reasoning_content` 选择启用推理内容重放，从而改善 LiteLLM/DeepSeek 等适配器中特定于提供商的推理/工具调用连续性。
-   修复了多个运行时和会话边界情况，包括 `SQLAlchemySession` 中并发执行的首次写入、移除推理内容后包含孤立助手消息 ID 的压缩请求、`remove_all_tools()` 遗留 MCP/推理项，以及工具调用批量执行器中的竞态问题。

### 0.12.0

此次次版本发布**不**引入破坏性变更。有关主要新增功能，请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)。

### 0.11.0

此次次版本发布**不**引入破坏性变更。有关主要新增功能，请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)。

### 0.10.0

此次次版本发布**不**引入破坏性变更，但包含一项面向 OpenAI Responses 用户的重要新功能：Responses API 的 websocket 传输支持。

亮点：

-   新增 OpenAI Responses 模型的 websocket 传输支持（需选择启用；HTTP 仍是默认传输方式）。
-   新增 `responses_websocket_session()` 辅助函数/`ResponsesWebSocketSession`，用于在多轮运行中复用支持 websocket 的共享提供商和 `RunConfig`。
-   新增 websocket 流式传输代码示例（`examples/basic/stream_ws.py`），涵盖流式传输、工具、审批和后续轮次。

### 0.9.0

在此版本中，不再支持 Python 3.9，因为该主要版本已于三个月前结束生命周期（EOL）。请升级到更新的运行时版本。

此外，`Agent#as_tool()` 方法返回值的类型提示已从 `Tool` 收窄为 `FunctionTool`。此变更通常不会引发破坏性问题，但如果您的代码依赖更宽泛的联合类型，可能需要进行一些调整。

### 0.8.0

在此版本中，两项运行时行为变更可能需要进行迁移：

- 封装**同步** Python 可调用对象的工具调用现在通过 `asyncio.to_thread(...)` 在工作线程上执行，而不再在事件循环线程上运行。如果您的工具逻辑依赖线程局部状态或具有线程亲和性的资源，请迁移到异步工具实现，或在工具代码中显式指定线程亲和性。
- 本地 MCP 工具失败处理现在可配置，且默认行为可以返回模型可见的错误输出，而不是使整个运行失败。如果您依赖快速失败语义，请设置 `mcp_config={"failure_error_function": None}`。服务级别的 `failure_error_function` 值会覆盖智能体级别的设置，因此请在每个具有显式处理程序的本地 MCP 服务上设置 `failure_error_function=None`。

### 0.7.0

在此版本中，有几项行为变更可能会影响现有应用：

- 嵌套任务转移历史现在需要**选择启用**（默认禁用）。如果您依赖 v0.6.x 的默认嵌套行为，请显式设置 `RunConfig(nest_handoff_history=True)`。
- `gpt-5.1`/`gpt-5.2` 的默认 `reasoning.effort` 已更改为 `"none"`（此前由 SDK 默认值配置为 `"low"`）。如果您的提示词或质量/成本配置依赖 `"low"`，请在 `model_settings` 中显式设置。

### 0.6.0

在此版本中，默认任务转移历史现在会打包到单条助手消息中，而不再公开原始的用户/助手轮次，从而为下游智能体提供简洁且可预测的回顾
- 现有的单消息任务转移对话记录现在默认在 `<CONVERSATION HISTORY>` 块之前以“For context, here is the conversation so far between the user and the previous agent:”开头，以便为下游智能体提供带有明确标签的回顾

### 0.5.0

此版本未引入任何可见的破坏性变更，但包含新功能和几项重要的底层更新：

- 新增对 `RealtimeRunner` 处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip)的支持
- 为兼容 Python 3.14，对 `Runner#run_sync` 的内部逻辑进行了大幅修改

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 软件包的 v1.x 版本。请将 openai v2.x 与此 SDK 配合使用。

### 0.3.0

在此版本中，Realtime API 支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，少数此前接受 `Agent` 作为参数的位置现在改为接受 `AgentBase`。例如，MCP 服务中的 `list_tools()` 调用。这纯粹是类型层面的变更，您仍会收到 `Agent` 对象。更新时，只需将 `Agent` 替换为 `AgentBase` 以修复类型错误。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。您需要将这些参数添加到任何继承 `MCPServer` 的类中。
