---
search:
  exclude: true
---
# 发布流程/变更日志

该项目遵循略有修改的语义化版本控制，采用 `0.Y.Z` 的形式。开头的 `0` 表示 SDK 仍在快速演进。各组成部分按如下方式递增：

## Minor（`Y`）版本

对于任何未标记为 beta 的公共接口中的**破坏性变更**，我们会递增 minor 版本 `Y`。例如，从 `0.0.x` 到 `0.1.x` 可能包含破坏性变更。

如果你不想遇到破坏性变更，我们建议在项目中固定使用 `0.0.x` 版本。

## Patch（`Z`）版本

对于非破坏性变更，我们会递增 `Z`：

-   Bug 修复
-   新功能
-   私有接口变更
-   beta 功能更新

## 破坏性变更日志

### 0.17.0

在此版本中，沙箱本地源材料化会将 `LocalFile.src` 和 `LocalDir.src` 保持在材料化 `base_dir` 内，除非源路径被 `Manifest.extra_path_grants` 覆盖。`base_dir` 是应用 manifest 时 SDK 进程的当前工作目录；相对本地源会从该目录解析，而绝对本地源必须已位于该目录内，或位于显式授权之下。这修复了一个本地制品边界问题，但可能会影响那些有意将受信任主机文件或目录从该基目录之外复制到沙箱工作区的应用程序。

要迁移，请在 manifest 级别使用 `SandboxPathGrant` 授权受信任的主机根目录；如果沙箱只需要读取这些文件，最好设为只读：

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

请将 `extra_path_grants` 视为受信任的应用程序配置。除非你的应用程序已批准这些主机路径，否则不要从模型输出或其他不受信任的 manifest 输入中填充授权。

### 0.16.0

在此版本中，SDK 默认模型现在是 `gpt-5.4-mini`，而不是 `gpt-4.1`。这会影响未显式设置模型的智能体和运行。由于新的默认值是 GPT-5 模型，隐式默认模型设置现在包含 GPT-5 默认值，例如 `reasoning.effort="none"` 和 `verbosity="low"`。

如果需要保留之前的默认模型行为，请在智能体或运行配置中显式设置模型，或设置 `OPENAI_DEFAULT_MODEL` 环境变量：

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

亮点：

-   `Runner.run`、`Runner.run_sync` 和 `Runner.run_streamed` 现在接受 `max_turns=None` 以禁用轮次限制。
-   沙箱工作区水合现在会拒绝包含指向归档根目录之外的符号链接的 tar 归档，包括绝对符号链接目标；这适用于本地、Docker 以及由提供方支持的沙箱实现。

### 0.15.0

在此版本中，模型拒绝现在会显式以 `ModelRefusalError` 暴露，而不是被当作空文本输出处理，或在 structured outputs 场景下导致运行循环重试直到 `MaxTurnsExceeded`。

这会影响此前预期仅包含拒绝的模型响应会以 `final_output == ""` 完成的代码。若要在不抛出异常的情况下处理拒绝，请提供 `model_refusal` 运行错误处理器：

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

对于 structured outputs 智能体，该处理器可以返回与智能体输出 schema 匹配的值，SDK 会像验证其他运行错误处理器最终输出一样验证它。

### 0.14.0

此 minor 版本**没有**引入破坏性变更，但新增了一个重要的 beta 功能领域：Sandbox Agents，以及在本地、容器化和托管环境中使用它们所需的运行时、后端和文档支持。

亮点：

-   新增了以 `SandboxAgent`、`Manifest` 和 `SandboxRunConfig` 为核心的 beta 沙箱运行时接口，使智能体能够在持久隔离工作区中处理文件、目录、Git 仓库、挂载、快照以及恢复支持。
-   通过 `UnixLocalSandboxClient` 和 `DockerSandboxClient` 新增了用于本地和容器化开发的沙箱执行后端，并通过可选 extras 提供了 Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop 和 Vercel 的托管提供方集成。
-   新增沙箱记忆支持，使未来运行可以复用先前运行中的经验，并支持渐进式披露、多轮分组、可配置隔离边界，以及包括 S3 支持工作流在内的持久化记忆示例。
-   新增了更广泛的工作区与恢复模型，包括本地和合成工作区条目、用于 S3/R2/GCS/Azure Blob Storage/S3 Files 的远程存储挂载、可移植快照，以及通过 `RunState`、`SandboxSessionState` 或已保存快照实现的恢复流程。
-   在 `examples/sandbox/` 下新增了大量沙箱示例和教程，涵盖使用 skills、任务转移、记忆、提供方特定设置的编码任务，以及代码审查、dataroom QA 和网站克隆等端到端工作流。
-   扩展了核心运行时和追踪栈，加入支持沙箱的会话准备、能力绑定、状态序列化、统一追踪、提示词缓存键默认值，以及更安全的敏感 MCP 输出遮蔽。

### 0.13.0

此 minor 版本**没有**引入破坏性变更，但包含一次值得注意的 Realtime 默认值更新，以及新的 MCP 能力和运行时稳定性修复。

亮点：

-   默认 websocket Realtime 模型现在是 `gpt-realtime-1.5`，因此新的 Realtime 智能体设置无需额外配置即可使用更新的模型。
-   `MCPServer` 现在公开 `list_resources()`、`list_resource_templates()` 和 `read_resource()`，并且 `MCPServerStreamableHttp` 现在公开 `session_id`，使可流式 HTTP 会话可以在重连或无状态 worker 之间恢复。
-   Chat Completions 集成现在可以通过 `should_replay_reasoning_content` 选择启用 reasoning-content replay，从而改善 LiteLLM/DeepSeek 等适配器的提供方特定推理/工具调用连续性。
-   修复了多个运行时和会话边界情况，包括 `SQLAlchemySession` 中并发首次写入、推理剥离后带有孤立 assistant 消息 ID 的压缩请求、`remove_all_tools()` 遗留 MCP/推理项，以及工具调用批处理执行器中的竞态问题。

### 0.12.0

此 minor 版本**没有**引入破坏性变更。主要功能新增请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)。

### 0.11.0

此 minor 版本**没有**引入破坏性变更。主要功能新增请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)。

### 0.10.0

此 minor 版本**没有**引入破坏性变更，但为 OpenAI Responses 用户包含了一个重要的新功能领域：Responses API 的 websocket 传输支持。

亮点：

-   为 OpenAI Responses 模型新增了 websocket 传输支持（选择启用；HTTP 仍为默认传输）。
-   新增了 `responses_websocket_session()` 辅助函数 / `ResponsesWebSocketSession`，用于在多轮运行之间复用共享的支持 websocket 的提供方和 `RunConfig`。
-   新增了一个 websocket 流式传输示例（`examples/basic/stream_ws.py`），涵盖流式传输、工具、审批和后续轮次。

### 0.9.0

在此版本中，Python 3.9 不再受支持，因为该主要版本已在三个月前达到 EOL。请升级到更新的运行时版本。

此外，`Agent#as_tool()` 方法返回值的类型提示已从 `Tool` 缩窄为 `FunctionTool`。此变更通常不会造成破坏性问题，但如果你的代码依赖更宽泛的联合类型，可能需要在你的代码侧进行一些调整。

### 0.8.0

在此版本中，两项运行时行为变更可能需要迁移工作：

- 包装**同步** Python 可调用对象的工具调用现在会通过 `asyncio.to_thread(...)` 在线程池线程上执行，而不是在事件循环线程上运行。如果你的工具逻辑依赖线程局部状态或线程亲和资源，请迁移到异步工具实现，或在工具代码中显式处理线程亲和性。
- 本地 MCP 工具失败处理现在可配置，默认行为可能返回对模型可见的错误输出，而不是使整个运行失败。如果你依赖快速失败语义，请设置 `mcp_config={"failure_error_function": None}`。服务级别的 `failure_error_function` 值会覆盖智能体级别设置，因此请在每个具有显式处理器的本地 MCP 服务上设置 `failure_error_function=None`。

### 0.7.0

在此版本中，有一些行为变更可能影响现有应用程序：

- 嵌套任务转移历史现在是**选择启用**（默认禁用）。如果你依赖 v0.6.x 默认的嵌套行为，请显式设置 `RunConfig(nest_handoff_history=True)`。
- `gpt-5.1` / `gpt-5.2` 的默认 `reasoning.effort` 已更改为 `"none"`（此前由 SDK 默认值配置的默认值为 `"low"`）。如果你的提示词或质量/成本配置依赖 `"low"`，请在 `model_settings` 中显式设置它。

### 0.6.0

在此版本中，默认任务转移历史现在会打包成一条 assistant 消息，而不是暴露原始的用户/assistant 轮次，从而为下游智能体提供简洁、可预测的回顾
- 现有的单消息任务转移转录现在默认会在 `<CONVERSATION HISTORY>` 块之前以“For context, here is the conversation so far between the user and the previous agent:”开头，使下游智能体获得清晰标注的回顾

### 0.5.0

此版本没有引入任何可见的破坏性变更，但包含一些新功能和若干重要的底层更新：

- 新增支持 `RealtimeRunner` 处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip)
- 为兼容 Python 3.14，大幅修订了 `Runner#run_sync` 的内部逻辑

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 包 v1.x 版本。请将 openai v2.x 与此 SDK 配合使用。

### 0.3.0

在此版本中，Realtime API 支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，过去使用 `Agent` 作为 arg 的若干位置，现在改为使用 `AgentBase` 作为 arg。例如，MCP 服务中的 `list_tools()` 调用。这只是类型层面的变更，你仍会收到 `Agent` 对象。要更新，只需通过将 `Agent` 替换为 `AgentBase` 来修复类型错误。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 有两个新 params：`run_context` 和 `agent`。你需要将这些 params 添加到任何继承 `MCPServer` 的类中。