---
search:
  exclude: true
---
# 发布流程/变更日志

本项目采用略作修改的语义化版本控制，版本格式为 `0.Y.Z`。开头的 `0` 表示 SDK 仍在快速演进。各组成部分按以下规则递增：

## 次版本（`Y`）

对于任何未标记为 beta 的公共接口，如果存在**破坏性变更**，我们将递增次版本号 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 时可能包含破坏性变更。

如果不希望遇到破坏性变更，建议在项目中将版本锁定为 `0.0.x`。

## 补丁版本（`Z`）

对于非破坏性变更，我们将递增 `Z`：

-   Bug 修复
-   新功能
-   私有接口变更
-   beta 功能更新

## 破坏性变更日志

### 0.18.0

此次次版本发布**没有**引入破坏性变更。递增次版本号仅用于更新 Realtime智能体的默认模型。

亮点：

-   Realtime智能体现在使用 `gpt-realtime-2.1` 作为默认模型，因此新的 Realtime 配置无需额外设置即可使用最新推荐模型。

### 0.17.0

在此版本中，沙箱本地源实体化会将 `LocalFile.src` 和 `LocalDir.src` 限制在实体化的 `base_dir` 内，除非源路径包含在 `Manifest.extra_path_grants` 中。应用清单时，`base_dir` 是 SDK 进程的当前工作目录；相对本地源路径从该目录解析，而绝对本地源路径必须已位于该目录内或显式授权的路径下。这修复了本地产物边界问题，但可能影响有意将该基础目录之外受信任的主机文件或目录复制到沙箱工作区的应用程序。

如需迁移，请使用 `SandboxPathGrant` 在清单级别授权受信任的主机根目录；如果沙箱只需读取这些文件，最好将其设为只读：

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

请将 `extra_path_grants` 视为受信任的应用程序配置。除非应用程序已批准相关主机路径，否则不要根据模型输出或其他不受信任的清单输入填充授权项。

### 0.16.0

在此版本中，SDK 默认模型已从 `gpt-4.1` 更改为 `gpt-5.4-mini`。这会影响未显式设置模型的智能体和运行。由于新的默认模型是 GPT-5 模型，隐式默认模型设置现在包含 GPT-5 的默认值，例如 `reasoning.effort="none"` 和 `verbosity="low"`。

如果需要保留之前的默认模型行为，请在智能体或运行配置中显式设置模型，或者设置 `OPENAI_DEFAULT_MODEL` 环境变量：

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

亮点：

-   `Runner.run`、`Runner.run_sync` 和 `Runner.run_streamed` 现在接受 `max_turns=None`，以禁用轮次限制。
-   在本地、Docker 和由提供商支持的沙箱实现中，沙箱工作区填充现在会拒绝包含指向归档根目录之外的符号链接（包括绝对符号链接目标）的 tar 归档。

### 0.15.0

在此版本中，模型拒绝现在会显式呈现为 `ModelRefusalError`，而不再被视为空文本输出；对于 structured outputs，也不会再导致运行循环持续重试，直至出现 `MaxTurnsExceeded`。

这会影响此前预期仅包含拒绝的模型响应以 `final_output == ""` 完成的代码。若要在不引发异常的情况下处理拒绝，请提供 `model_refusal` 运行错误处理程序：

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

对于使用 structured outputs 的智能体，处理程序可以返回与智能体输出模式匹配的值，SDK 将像验证其他运行错误处理程序的最终输出一样对其进行验证。

### 0.14.0

此次次版本发布**没有**引入破坏性变更，但新增了一个重要的 beta 功能领域：沙箱智能体，以及在本地、容器化和托管环境中使用它们所需的运行时、后端和文档支持。

亮点：

-   新增以 `SandboxAgent`、`Manifest` 和 `SandboxRunConfig` 为核心的 beta 沙箱运行时接口，使智能体能够在持久化的隔离工作区中处理文件、目录、Git 仓库、挂载、快照，并支持恢复。
-   新增通过 `UnixLocalSandboxClient` 和 `DockerSandboxClient` 支持本地及容器化开发的沙箱执行后端，并通过可选扩展集成 Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop 和 Vercel 等托管提供商。
-   新增沙箱记忆支持，使后续运行可以复用先前运行中的经验，并支持渐进式披露、多轮分组、可配置的隔离边界，以及包含 S3 后端工作流的持久化记忆代码示例。
-   新增更广泛的工作区和恢复模型，包括本地及合成工作区条目、用于 S3/R2/GCS/Azure Blob Storage/S3 Files 的远程存储挂载、可移植快照，以及通过 `RunState`、`SandboxSessionState` 或已保存快照实现的恢复流程。
-   在 `examples/sandbox/` 下新增大量沙箱代码示例和教程，涵盖结合技能、任务转移和记忆的编码任务、特定提供商的配置，以及代码审查、数据室问答和网站克隆等端到端工作流。
-   扩展核心运行时和追踪技术栈，新增沙箱感知的会话准备、能力绑定、状态序列化、统一追踪、提示词缓存键默认值，以及更安全的敏感 MCP 输出脱敏。

### 0.13.0

此次次版本发布**没有**引入破坏性变更，但包含一项值得关注的 Realtime 默认设置更新、新的 MCP 功能以及运行时稳定性修复。

亮点：

-   默认 websocket Realtime 模型现在是 `gpt-realtime-1.5`，因此新的 Realtime智能体配置无需额外设置即可使用更新的模型。
-   `MCPServer` 现在公开 `list_resources()`、`list_resource_templates()` 和 `read_resource()`，而 `MCPServerStreamableHttp` 现在公开 `session_id`，从而使可流式传输的 HTTP 会话能够在重新连接或无状态工作进程之间恢复。
-   Chat Completions集成现在可以通过 `should_replay_reasoning_content` 选择启用推理内容重放，从而改善 LiteLLM/DeepSeek 等适配器中特定于提供商的推理/工具调用连续性。
-   修复多个运行时和会话边界情况，包括 `SQLAlchemySession` 中并发的首次写入、移除推理内容后包含孤立助手消息 ID 的压缩请求、`remove_all_tools()` 遗留 MCP/推理项，以及工具调用批量执行器中的竞态条件。

### 0.12.0

此次次版本发布**没有**引入破坏性变更。有关主要新增功能，请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)。

### 0.11.0

此次次版本发布**没有**引入破坏性变更。有关主要新增功能，请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)。

### 0.10.0

此次次版本发布**没有**引入破坏性变更，但为 OpenAI Responses用户新增了一个重要功能领域：Responses API 的 websocket 传输支持。

亮点：

-   新增对 OpenAI Responses模型的 websocket 传输支持（需选择启用；HTTP 仍为默认传输方式）。
-   新增 `responses_websocket_session()` 辅助函数 / `ResponsesWebSocketSession`，用于在多轮运行中复用支持 websocket 的共享提供商和 `RunConfig`。
-   新增 websocket 流式传输代码示例（`examples/basic/stream_ws.py`），涵盖流式传输、工具、审批和后续轮次。

### 0.9.0

在此版本中，不再支持 Python 3.9，因为该主要版本已于三个月前终止生命周期。请升级到更新的运行时版本。

此外，`Agent#as_tool()` 方法返回值的类型提示已从 `Tool` 收窄为 `FunctionTool`。此变更通常不会导致破坏性问题，但如果代码依赖更宽泛的联合类型，可能需要进行一些调整。

### 0.8.0

在此版本中，两项运行时行为变更可能需要迁移：

- 包装**同步** Python 可调用对象的工具调用现在通过 `asyncio.to_thread(...)` 在工作线程上执行，而不再在事件循环线程上运行。如果工具逻辑依赖线程局部状态或具有线程亲和性的资源，请迁移到异步工具实现，或在工具代码中显式指定线程亲和性。
- 本地 MCP 工具的故障处理现在可配置，默认行为可以返回模型可见的错误输出，而不是使整个运行失败。如果依赖快速失败语义，请设置 `mcp_config={"failure_error_function": None}`。服务级别的 `failure_error_function` 值会覆盖智能体级别的设置，因此请在每个具有显式处理程序的本地 MCP服务上设置 `failure_error_function=None`。

### 0.7.0

在此版本中，有几项行为变更可能会影响现有应用程序：

- 嵌套任务转移历史记录现在需要**选择启用**（默认禁用）。如果依赖 v0.6.x 默认的嵌套行为，请显式设置 `RunConfig(nest_handoff_history=True)`。
- `gpt-5.1` / `gpt-5.2` 的默认 `reasoning.effort` 已从 SDK 默认设置所配置的 `"low"` 更改为 `"none"`。如果提示词或质量/成本配置依赖 `"low"`，请在 `model_settings` 中显式设置该值。

### 0.6.0

在此版本中，默认任务转移历史记录现在会打包为一条助手消息，而不再公开原始的用户/助手轮次，从而为下游智能体提供简洁且可预测的回顾
- 现有的单消息任务转移记录现在默认在 `<CONVERSATION HISTORY>` 块之前以“作为上下文，以下是用户与前一个智能体之间截至目前的对话：”开头，从而为下游智能体提供带有清晰标签的回顾

### 0.5.0

此版本未引入任何可见的破坏性变更，但包含新功能和几项重要的底层更新：

- 新增对 `RealtimeRunner` 处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip)的支持
- 为兼容 Python 3.14，对 `Runner#run_sync` 的内部逻辑进行了重大修订

### 0.4.0

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 软件包的 v1.x 版本。请将 openai v2.x 与此 SDK 配合使用。

### 0.3.0

在此版本中，Realtime API支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0

在此版本中，一些过去接受 `Agent` 作为参数的位置现在改为接受 `AgentBase`。例如，MCP服务中的 `list_tools()` 调用。这仅是类型变更，仍会收到 `Agent` 对象。更新时，只需将 `Agent` 替换为 `AgentBase` 以修复类型错误。

### 0.1.0

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增两个参数：`run_context` 和 `agent`。需要将这些参数添加到所有继承 `MCPServer` 的类中。