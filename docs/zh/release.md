---
search:
  exclude: true
---
# 发布流程/变更日志

本项目采用略作修改的语义化版本控制，格式为 `0.Y.Z`。开头的 `0` 表示 SDK 仍在快速演进。各部分按以下方式递增：

## 次版本（`Y`） {#minor-y-versions}

对于任何未标记为 beta 的公共接口发生的**破坏性变更**，我们会递增次版本 `Y`。例如，从 `0.0.x` 升级到 `0.1.x` 时可能包含破坏性变更。

如果您不希望引入破坏性变更，建议在项目中固定使用 `0.0.x` 版本。

## 补丁版本（`Z`） {#patch-z-versions}

对于非破坏性变更，我们会递增 `Z`：

-   错误修复
-   新功能
-   私有接口变更
-   beta 功能更新

## 破坏性变更日志 {#breaking-change-changelog}

### 0.22.0 {#0220}

版本 0.22.0 加强了多个现有 API 的失败处理和数据隔离。使用显式客户端构造 `OpenAIProvider`，同时还向提供商传递 `organization` 或 `project` 的应用程序，必须移除这些重复参数。

要点：

-   当智能体级输出安全防护措施阻止由终止函数工具直接生成的最终输出时，仅当经过验证的字段允许安全重建时，SDK 才会保留可用于重放的调用/输出对。原始 `function_call_output` 载荷会在会话历史记录、`RunState` 和流式结果状态中替换为固定文本 `"Output withheld by an output guardrail."`，而包含载荷的当前响应安全防护措施元数据会被清除或替换。如果当前响应包含推理内容或其他不受支持的结构，SDK 会改为丢弃完整的当前响应后缀。此前已接受的轮次和安全防护措施结果仍然可用。请参阅[输出安全防护措施](guardrails.md#output-guardrails)。
-   对于非流式 OpenAI Responses 调用，当返回响应的终止状态为 `failed` 或 `incomplete` 时，现在会引发 `ModelBehaviorError`，与现有的流式终止事件处理方式一致。这适用于 `OpenAIResponsesModel` 以及 `AnyLLMModel` 中的 Responses 路径。请参阅[异常](running_agents.md#exceptions)。
-   当 `openai_client` 与 `organization` 或 `project` 结合使用时，[`OpenAIProvider`][agents.models.openai_provider.OpenAIProvider] 现在也会引发 `UserError`。与 `api_key`、`base_url` 和 `websocket_base_url` 的现有冲突保持不变。请改为在显式 `AsyncOpenAI` 客户端上配置这些值。请参阅 [API 密钥和客户端](config.md#api-keys-and-clients)。
-   每个 `RunResult.to_state()` 检查点现在都拥有独立的用量快照。恢复后的结果以检查点总量为起点，并累加自身的模型调用，而不会修改源结果或同级检查点。嵌套的 `Agent.as_tool()` 恢复仍会将恢复后的用量汇总到当前活跃的外层运行中。请参阅 [RunState 检查点中的用量](usage.md#usage-in-runstate-checkpoints)。
-   智能体可视化现在会递归展开通过 `handoff(agent)` 注册的目标所包含的工具、MCP服务器和下游任务转移，其行为与智能体 `handoffs` 列表中的直接 `Agent` 条目一致。请参阅[图形生成](visualization.md#generating-a-graph)。
-   `Agent.clone()` 和 `RealtimeAgent.clone()` 的 API 指南现在准确说明了其现有的浅拷贝行为：未被覆盖的列表属性仍是相同的列表对象。如果克隆对象必须独立拥有该容器，请传入新列表。请参阅[智能体的克隆/复制](agents.md#cloningcopying-agents)。

### 0.21.0 {#0210}

版本 0.21.0 要求使用 `openai` v3，并将 Agents SDK的OpenAI HTTP 集成迁移到 HTTPX2。使用默认 OpenAI客户端的应用程序无需更改客户端设置，但自定义 OpenAI HTTP 层的应用程序可能需要迁移面向传输层的代码。

要点：

-   现在要求的 OpenAI依赖项为 `openai>=3.0.0,<4`。全新的核心安装使用 HTTPX2，并且不再将旧版 `httpx` 作为直接依赖项安装。
-   默认 OpenAI提供商、语音提供商、Responses WebSocket 支持、追踪导出器和提供商重试规范化现在使用 HTTPX2。其现有的 Agents SDK公共配置和运行时行为保持不变。
-   向 `AsyncOpenAI` 传递 `http_client=` 的应用程序，应将自定义客户端、传输、身份验证、事件钩子、模拟传输、超时值、URL、请求、响应和传输异常处理从 `httpx` 迁移到 `httpx2`。如果应用程序既需要 OpenAI客户端的默认设置，又需要自定义 HTTP 选项，请优先使用 OpenAI Python SDK的 `DefaultAsyncHttpx2Client`。请参阅[使用 `openai` v3 的自定义 HTTP 客户端](config.md#custom-http-clients-with-openai-v3)。
-   Agents SDK不会将任意旧版 HTTPX 对象转换为 HTTPX2。OpenAI Python SDK的临时旧版客户端兼容路径要求显式安装 `httpx`，并且应将其视为迁移过渡方案。
-   本地 MCP HTTP 自定义继续遵循已安装的 MCP软件包：MCP Python SDK v1 提供并使用旧版 `httpx`，而 MCP Python SDK v2 使用 `httpx2`。普通 MCP连接无需更改应用程序。请参阅 [MCP Python SDK v1 和 v2](mcp.md#mcp-python-sdk-v1-and-v2)。
-   公共的提供商中立测试实用工具现在无需依赖提供商或进程，即可覆盖智能体模型、沙箱会话、Realtime 会话和语音管线工作流。有关操作方法以及何时应保留真实提供商适配器或集成边界的指南，请参阅[测试](testing.md)。

### 0.20.0 {#0200}

版本 0.20.0 包含一项可能具有破坏性的 MCP依赖项迁移，影响自定义本地 MCP HTTP 传输的应用程序。它还更新了智能体或运行未显式选择模型时所使用的 SDK 默认模型。

要点：

-   SDK 默认模型现在是 `gpt-5.6-luna`，而不再是 `gpt-5.4-mini`。默认的 `reasoning.effort="none"` 和 `verbosity="low"` 设置保持不变。
-   显式指定的智能体模型、运行级模型覆盖以及 `OPENAI_DEFAULT_MODEL` 环境变量仍然优先于 SDK 默认值。
-   Realtime 输入转录设置现在可识别 `gpt-transcribe`、`gpt-live-transcribe` 和 `gpt-realtime-whisper`。对于低延迟 `gpt-live-transcribe` 会话，嵌套的 `audio.input.transcription` 设置可以提供 `prompt`、`keywords` 和多个预期的 `languages`。此 SDK 固定使用的 OpenAI客户端版本仅在搭配 `gpt-realtime-whisper` 时支持 `delay` 延迟/准确度级别。若要在提交音频轮次后进行转录，或获取检测到的语言输出，请通过 WebSocket 使用 `gpt-transcribe`。显式设置 `audio.input.turn_detection=None` 会禁用自动轮次检测。请参阅[输入转录设置](realtime/guide.md#input-transcription-settings)。
-   由 Agents SDK创建的本地 MCP连接现在支持 MCP Python SDK v2，同时通过 `mcp>=1.19.0,<3` 保持与 v1 的兼容性。Agents SDK会自动适配普通的 stdio、SSE 和 Streamable HTTP 连接。安装 MCP v2 后，这些连接会使用 `mcp.Client(mode="auto")` 探测最新受支持的协议，并针对旧版服务器回退到传统的 `initialize` 握手。如果依赖项解析选择了 MCP v2，则提供自定义 `httpx.Auth` 对象或 `httpx.AsyncClient` 工厂的应用程序必须将这些值迁移到 `httpx2`，或者固定使用 `mcp<2` 以保留 v1 HTTP 栈。`MCPServerStreamableHttp` 的 `params["ignore_initialized_notification_failure"] = True` 选项也仍然仅支持 v1。有关迁移详情，请参阅 [MCP Python SDK v1 和 v2](mcp.md#mcp-python-sdk-v1-and-v2)。
-   沙箱挂载验证现在会在产生沙箱或挂载辅助程序的副作用之前，拒绝不安全的凭证放置方式。受信任的应用程序可以针对容器内的确切挂载路径，确认挂载范围内或广泛的凭证暴露，而无需更改存储能力表。这些确认仅在运行时有效，序列化的沙箱状态本身绝不会授予凭证权限。在受保护的挂载边界处，SDK 会返回一个新的、已脱敏的异常。如果源异常是可明确识别的 SDK 沙箱错误，并且其获准的结构化字段通过验证，则替代异常会保留该子类型和经过验证的安全字段。可识别的 `MountConfigError` 也可以保留由 SDK 生成的安全验证消息。否则，SDK 会返回一个新的通用脱敏错误。由提供商控制或未经批准的消息、命令数据、注释、上下文、原因和源回溯状态均不会保留。请参阅[挂载与远程存储](sandbox/clients.md#mounts-and-remote-storage)和[从会话状态恢复](sandbox/guide.md#resume-from-session-state)。
-   重试策略可以检查稳定的重放安全事实，并为提供商标记为不安全的非流式请求显式设置 `RetryDecision(approve_unsafe_replay=True)`。此批准不会绕过中止、已发出的流式输出，也不会绕过诸如程序化工具调用等单独的本地副作用否决。请参阅[由 Runner 管理的重试](models/index.md#runner-managed-retries)。
-   可恢复的 `RunState` 对象现在可以在下次模型调用之前，使用 `add_input()` 暂存持久化用户输入。暂存的输入可在序列化后保留，会经过输入安全防护措施，并在本地会话和服务器管理的对话中产生一次持久化 SDK 输入记录。显式批准的不安全重放仍可能向提供商重新发送输入，并重复提供商侧的工作。请参阅[恢复前添加输入](results.md#add-input-before-resuming)。
-   运行时可靠性修复统一了流式和非流式的[输出安全防护措施会话持久化](guardrails.md#output-guardrails)，在复制和命名空间处理期间保留 `FunctionTool` 子类，并针对[不受支持的 Chat Completions 音频输出](models/index.md#chat-completions-compatibility-options)引发显式错误，而不是静默完成空流。`OpenAIResponsesCompactionSession` 包装器会在取消操作到达调用方之前，尝试并等待[压缩前历史记录恢复](sessions/index.md#auto-compaction-can-block-streaming)。[`VoicePipeline`](voice/pipeline.md#results) 使用方现在会在运行正常结束后收到转录会话关闭失败；如果某个轮次更早发生失败，则该失败的优先级高于之后的关闭失败。`RunState` 往返转换现在会保留本地 shell 输出、已确认的计算机安全检查、采用默认值的工具输出字段，以及遍历字典、列表或元组时遇到的 Pydantic 模型或数据类输出。MCP转换会保留自由形式的对象 schema 和图像输出，并将音频块、资源块等其他原始内容块序列化为有效的 JSON 文本。`MCPServerManager` 会对重叠的生命周期操作进行串行化，并为连接和清理应用有限的默认超时。模型重放会先从输出项中移除服务器拥有的 `created_by` 元数据，再将其用作输入。

### 0.19.0 {#0190}

此次次版本发布**不会**引入破坏性变更。次版本号递增是因为新增了一个重要的 OpenAI Responses 功能领域：程序化工具调用。

要点：

-   新增 [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool]，支持的 OpenAI Responses模型可通过它生成 JavaScript，以协调符合程序化工具调用条件的工具。它支持每个工具的 `allowed_callers`、来自 `FunctionTool` 实例的 structured outputs，以及与 Runner 流式传输、安全防护措施、审批、会话和 `RunState` 的集成。有关设置和约束，请参阅[程序化工具调用](tools.md#programmatic-tool-calling)。
-   新增公共 `agents.decorators` 模块，并增加 `@tool`，作为现有 `@function_tool` 装饰器的较短别名，同时保留现有安全防护措施装饰器。`FunctionTool` 实例现在也支持异步可调用对象。
-   现在，SDK 配置可在智能体、运行、模型、会话、沙箱和语音管线中一致地接受带类型的设置对象或字典，并会验证未知设置。
-   强化了模型、工具、MCP、Realtime、会话、沙箱和追踪中的错误及诊断日志记录，在保留有用调试上下文的同时避免暴露原始敏感载荷。
-   改进了 AnyLLM、LiteLLM 和 Chat Completions 的兼容性，在模型重试期间保留会话历史记录，并为响应开始前发生的 WebSocket 过载添加了提供商重试指南，使选择启用的 Runner 重试策略可以在获准时重放失败的尝试。
-   通过 `VercelCloudBucketMountStrategy` 新增了[仅能在创建 Vercel 沙箱时配置的 S3 挂载](sandbox/clients.md#mounts-and-remote-storage)。已挂载的会话会从工作区持久化中排除存储桶内容，并且有意不支持动态挂载变更或会话恢复。

### 0.18.0 {#0180}

此次次版本发布**不会**引入破坏性变更。次版本号递增仅用于更新 Realtime 智能体的默认模型。

要点：

-   Realtime 智能体现在使用 `gpt-realtime-2.1` 作为默认模型，因此新的 Realtime 设置无需额外配置即可使用最新的推荐模型。

### 0.17.0 {#0170}

在此版本中，除非源路径由 `Manifest.extra_path_grants` 覆盖，否则沙箱本地源实体化会将 `LocalFile.src` 和 `LocalDir.src` 限制在实体化 `base_dir` 内。应用清单时，`base_dir` 是 SDK 进程的当前工作目录；相对本地源从该目录解析，而绝对本地源必须已经位于其中或位于显式授权的路径下。此变更修复了本地产物边界问题，但可能影响有意将该基础目录之外的受信任主机文件或目录复制到沙箱工作区的应用程序。

若要迁移，请使用 `SandboxPathGrant` 在清单级别授予对受信任主机根目录的访问权限；如果沙箱只需读取这些文件，最好授予只读权限：

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

请将 `extra_path_grants` 视为受信任的应用程序配置。除非应用程序已批准这些主机路径，否则不要根据模型输出或其他不受信任的清单输入填充授权。

### 0.16.0 {#0160}

在此版本中，SDK 默认模型现在是 `gpt-5.4-mini`，而不再是 `gpt-4.1`。这会影响未显式设置模型的智能体和运行。由于新的默认模型是 GPT-5 模型，隐式默认模型设置现在包括 `reasoning.effort="none"` 和 `verbosity="low"` 等 GPT-5 默认值。

如果您需要保留之前的默认模型行为，请在智能体或运行配置中显式设置模型，或者设置 `OPENAI_DEFAULT_MODEL` 环境变量：

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

要点：

-   `Runner.run`、`Runner.run_sync` 和 `Runner.run_streamed` 现在接受 `max_turns=None`，以禁用轮次限制。
-   对于本地、Docker 和提供商支持的沙箱实现，沙箱工作区填充现在会拒绝包含指向归档根目录之外的符号链接的 tar 归档，其中也包括目标为绝对路径的符号链接。

### 0.15.0 {#0150}

在此版本中，模型拒绝现在会显式呈现为 `ModelRefusalError`，而不会被视为空文本输出；对于 structured outputs，也不会再导致运行循环持续重试直至 `MaxTurnsExceeded`。

这会影响此前预期仅包含拒绝的模型响应以 `final_output == ""` 完成的代码。若要处理拒绝而不引发异常，请提供 `model_refusal` 运行错误处理程序：

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

对于使用 structured outputs 的智能体，处理程序可以返回与智能体输出 schema 匹配的值，SDK 会像验证其他运行错误处理程序的最终输出一样验证该值。

### 0.14.0 {#0140}

此次次版本发布**不会**引入破坏性变更，但新增了一个重要的 beta 功能领域：沙箱智能体，以及在本地、容器化和托管环境中使用它们所需的运行时、后端和文档支持。

要点：

-   新增以 `SandboxAgent`、`Manifest` 和 `SandboxRunConfig` 为核心的 beta 沙箱运行时接口，使智能体能够在持久化的隔离工作区内处理文件、目录、Git 仓库、挂载和快照，并支持恢复。
-   新增通过 `UnixLocalSandboxClient` 和 `DockerSandboxClient` 实现的本地及容器化开发沙箱执行后端，并通过 Python 软件包中的可选依赖 extras，为 Blaxel、Cloudflare、Daytona、E2B、Modal、Runloop 和 Vercel 提供托管提供商集成。
-   新增沙箱记忆支持，使未来的运行能够复用以往运行中获得的经验，并提供渐进式披露、多轮分组、可配置的隔离边界，以及包括 S3 支持工作流在内的持久化记忆代码示例。
-   新增更广泛的工作区和恢复模型，包括本地及合成工作区条目、S3/R2/GCS/Azure Blob Storage/S3 Files 的远程存储挂载、可移植快照，以及通过 `RunState`、`SandboxSessionState` 或已保存快照实现的恢复流程。
-   在 `examples/sandbox/` 下新增大量沙箱代码示例和教程，涵盖使用技能、任务转移和记忆完成编码任务、提供商专用设置，以及代码审查、数据室问答和网站克隆等端到端工作流。
-   扩展了核心运行时和追踪栈，新增沙箱感知的会话准备、能力绑定、状态序列化、统一追踪、提示词缓存键默认值，以及更安全的敏感 MCP输出脱敏。

### 0.13.0 {#0130}

此次次版本发布**不会**引入破坏性变更，但包含一项值得注意的 Realtime 默认值更新、新的 MCP能力以及运行时稳定性修复。

要点：

-   默认 websocket Realtime 模型现在是 `gpt-realtime-1.5`，因此新的 Realtime 智能体设置无需额外配置即可使用更新的模型。
-   `MCPServer` 现在公开 `list_resources()`、`list_resource_templates()` 和 `read_resource()`，而 `MCPServerStreamableHttp` 现在公开 `session_id`，因此使用 MCP Streamable HTTP 传输的会话可以在重新连接后或无状态工作进程之间恢复。
-   Chat Completions 集成现在可以通过 `should_replay_reasoning_content` 选择重新发送现有推理内容，从而改善 LiteLLM/DeepSeek 等适配器中特定于提供商的推理/工具调用连续性。
-   修复了多个运行时和会话边界情况，包括 `SQLAlchemySession` 中的并发首次写入、移除推理内容后带有孤立助手消息 ID 的压缩请求、`remove_all_tools()` 遗留 MCP/推理项，以及 `FunctionTool` 实例批处理执行器中的竞态条件。

### 0.12.0 {#0120}

此次次版本发布**不会**引入破坏性变更。有关主要新增功能，请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)。

### 0.11.0 {#0110}

此次次版本发布**不会**引入破坏性变更。有关主要新增功能，请查看[发布说明](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)。

### 0.10.0 {#0100}

此次次版本发布**不会**引入破坏性变更，但为 OpenAI Responses用户新增了一个重要功能领域：Responses API 的 websocket 传输支持。

要点：

-   新增对 OpenAI Responses模型的 websocket 传输支持（选择启用；HTTP 仍是默认传输）。
-   新增 `responses_websocket_session()` 辅助函数/`ResponsesWebSocketSession`，用于在多轮运行中复用支持 websocket 的共享提供商和 `RunConfig`。
-   新增 websocket 流式传输代码示例（`examples/basic/stream_ws.py`），涵盖流式传输、工具、审批和后续轮次。

### 0.9.0 {#090}

在此版本中，不再支持 Python 3.9，因为该主要版本已于三个月前终止支持。请升级到较新的运行时版本。

此外，`Agent#as_tool()` 方法返回值的类型提示已从 `Tool` 收窄为 `FunctionTool`。此变更通常不会造成破坏性问题，但如果您的代码依赖更宽泛的联合类型，可能需要进行一些调整。

### 0.8.0 {#080}

在此版本中，两项运行时行为变更可能需要迁移：

- 包装**同步** Python 可调用对象的 `FunctionTool` 实例现在会通过 `asyncio.to_thread(...)` 在工作线程中执行，而不再在事件循环线程上运行。如果您的工具逻辑依赖线程局部状态或具有线程亲和性的资源，请迁移到异步工具实现，或在工具代码中显式指定线程亲和性。
- 本地 MCP工具失败处理现在可以配置，并且默认行为可以返回模型可见的错误输出，而不是使整个运行失败。如果您依赖快速失败语义，请设置 `mcp_config={"failure_error_function": None}`。服务器级 `failure_error_function` 值会覆盖智能体级设置，因此请在每个具有显式处理程序的本地 MCP服务器上设置 `failure_error_function=None`。

### 0.7.0 {#070}

在此版本中，有几项行为变更可能会影响现有应用程序：

- 嵌套任务转移历史记录现在需要**选择启用**（默认禁用）。如果您依赖 v0.6.x 的默认嵌套行为，请显式设置 `RunConfig(nest_handoff_history=True)`。
- `gpt-5.1`/`gpt-5.2` 的默认 `reasoning.effort` 已更改为 `"none"`（之前是由 SDK 默认值配置的 `"low"`）。如果您的提示词或质量/成本配置依赖 `"low"`，请在 `model_settings` 中显式设置它。

### 0.6.0 {#060}

在此版本中，默认任务转移历史记录现在会封装为一条助手消息，而不再将用户和助手轮次作为单独消息传递，从而为下游智能体提供简洁且可预测的摘要
- 现有的单消息任务转移记录现在默认会在 `<CONVERSATION HISTORY>` 块之前，以完全一致的字面文本 `For context, here is the conversation so far between the user and the previous agent:` 开头，以便下游智能体获得带有清晰标签的摘要

### 0.5.0 {#050}

此版本不会引入任何可见的破坏性变更，但包含新功能和若干重要的底层更新：

- 在 `RealtimeRunner` 中新增对处理 [SIP 协议连接](https://platform.openai.com/docs/guides/realtime-sip)的支持。
- 大幅修订了 `Runner#run_sync` 的内部逻辑，以兼容 Python 3.14

### 0.4.0 {#040}

在此版本中，不再支持 [openai](https://pypi.org/project/openai/) 软件包 v1.x 版本。请将 openai v2.x 与此 SDK 搭配使用。

### 0.3.0 {#030}

在此版本中，Realtime API支持迁移到 gpt-realtime 模型及其 API 接口（GA 版本）。

### 0.2.0 {#020}

在此版本中，之前有几处接受 `Agent` 作为参数的位置，现在改为接受 `AgentBase`。例如，这适用于 MCP服务器中的 `list_tools()` 方法签名。这只是类型方面的变更，您仍会收到 `Agent` 对象。若要更新，只需将 `Agent` 替换为 `AgentBase`，以修复类型错误。

### 0.1.0 {#010}

在此版本中，[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] 新增了两个参数：`run_context` 和 `agent`。您需要将这些参数添加到 `MCPServer` 子类中每个被重写的 `MCPServer.list_tools()` 方法。