---
search:
  exclude: true
---
# 快速入门

!!! warning "Beta 功能"

    沙箱智能体目前处于 Beta 阶段。在正式发布之前，API 细节、默认设置和支持的能力可能会发生变化，后续也将逐步提供更高级的功能。

现代智能体只有能够操作文件系统中的真实文件，才能发挥最佳效果。Agents SDK 中的**沙箱智能体**为模型提供持久化工作区，使其能够检索大型文档集、编辑文件、运行命令、生成产物，并从保存的沙箱状态继续工作。

SDK 提供了这套执行框架，无需你自行整合文件暂存、文件系统工具、Shell 访问、沙箱生命周期、快照以及特定于提供商的适配逻辑。你可以继续使用常规的 `Agent` 和 `Runner` 流程，然后添加用于工作区的 `Manifest`、沙箱原生工具所需的能力，以及用于指定工作运行位置的 `SandboxRunConfig`。

## 前置条件 {#prerequisites}

- Python 3.10 或更高版本
- 基本熟悉 OpenAI Agents SDK
- 一个沙箱客户端。进行本地开发时，可从 `UnixLocalSandboxClient` 开始。

## 安装 {#installation}

如果尚未安装 SDK：

```bash
pip install openai-agents
```

对于由 Docker 支持的沙箱：

```bash
pip install "openai-agents[docker]"
```

## 本地沙箱智能体的创建 {#create-a-local-sandbox-agent}

此代码示例将本地仓库存放到 `repo/` 下，按需延迟加载本地技能，并让运行器为本次运行创建 Unix 本地沙箱会话。

```python
import asyncio
from pathlib import Path

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Capabilities, LocalDirLazySkillSource, Skills
from agents.sandbox.entries import LocalDir
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

EXAMPLE_DIR = Path(__file__).resolve().parent
HOST_REPO_DIR = EXAMPLE_DIR / "repo"
HOST_SKILLS_DIR = EXAMPLE_DIR / "skills"


def build_agent(model: str) -> SandboxAgent[None]:
    return SandboxAgent(
        name="Sandbox engineer",
        model=model,
        instructions=(
            "Read `repo/task.md` before editing files. Stay grounded in the repository, preserve "
            "existing behavior, and mention the exact verification command you ran. "
            "If you edit files with apply_patch, paths are relative to the sandbox workspace root."
        ),
        default_manifest=Manifest(
            entries={
                "repo": LocalDir(src=HOST_REPO_DIR),
            }
        ),
        capabilities=Capabilities.default() + [
            Skills(
                lazy_from=LocalDirLazySkillSource(
                    # This is a host path read by the SDK process.
                    # Requested skills are copied into `skills_path` in the sandbox.
                    source=LocalDir(src=HOST_SKILLS_DIR),
                )
            ),
        ],
    )


async def main() -> None:
    result = await Runner.run(
        build_agent("gpt-5.6-sol"),
        "Open `repo/task.md`, fix the issue, run the targeted test, and summarize the change.",
        run_config=RunConfig(
            sandbox=SandboxRunConfig(client=UnixLocalSandboxClient()),
            workflow_name="Sandbox coding example",
        ),
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

请参阅 [examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py)。它使用一个基于 Shell 的微型仓库，因此可在不同的 Unix 本地运行中以确定性方式验证该代码示例。

## 关键选项 {#key-choices}

基本运行正常后，大多数人接下来会使用以下选项：

- `default_manifest`：用于新沙箱会话的文件、仓库、目录和挂载
- `instructions`：应适用于不同提示词的简短工作流规则
- `base_instructions`：用于替换 SDK 沙箱提示词的高级扩展入口
- `capabilities`：沙箱原生工具，例如文件系统编辑、图像检查、Shell、技能、记忆，以及 SDK 的压缩机制
- `run_as`：面向模型的工具执行时使用的沙箱用户账户
- `SandboxRunConfig.client`：沙箱后端
- `SandboxRunConfig.session`、`session_state` 或 `snapshot`：后续运行重新连接到先前工作的方式

## 后续步骤 {#where-to-go-next}

- [概念](sandbox/guide.md)：了解清单、能力、权限、快照、运行配置和组合模式。
- [沙箱客户端](sandbox/clients.md)：选择 Unix 本地、Docker、托管提供商和挂载策略。
- [智能体记忆](sandbox/memory.md)：保留并复用之前沙箱运行中获得的经验。

如果 Shell 访问只是你偶尔使用的一项工具，请先从[工具指南](tools.md)中的托管 Shell 开始。当工作区隔离、沙箱客户端选择或沙箱会话恢复行为属于设计的一部分时，请使用沙箱智能体。