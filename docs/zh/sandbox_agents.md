---
search:
  exclude: true
---
# 快速开始

!!! warning "Beta 功能"

    Sandbox 智能体目前处于 beta 阶段。预计 API 的细节、默认设置和支持的能力会在正式可用前发生变化，并且功能也会随着时间推移变得更高级。

现代智能体在能够对文件系统中的真实文件进行操作时效果最佳。Agents SDK 中的 **Sandbox Agents** 为模型提供了一个持久化工作区，模型可以在其中检索大型文档集、编辑文件、运行命令、生成产物，并从已保存的 sandbox 状态中继续工作。

SDK 为你提供了这一执行框架，无需你自己去拼接文件暂存、文件系统工具、shell 访问、sandbox 生命周期、快照以及特定提供方的胶水代码。你可以保留常规的 `Agent` 和 `Runner` 流程，然后再为工作区添加 `Manifest`、用于 sandbox 原生工具的 capabilities，以及用于指定工作运行位置的 `SandboxRunConfig`。

## 前提条件

- Python 3.10 或更高版本
- 对 OpenAI Agents SDK 有基本了解
- 一个 sandbox 客户端。对于本地开发，建议从 `UnixLocalSandboxClient` 开始。

## 安装

如果你尚未安装 SDK：

```bash
pip install openai-agents
```

对于由 Docker 支持的 sandboxes：

```bash
pip install "openai-agents[docker]"
```

## 创建本地 sandbox 智能体

此示例会将本地仓库暂存到 `repo/` 下，按需延迟加载本地 skills，并让 runner 为本次运行创建一个 Unix 本地 sandbox 会话。

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
        build_agent("gpt-5.4"),
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

参见[examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py)。它使用了一个基于 shell 的小型仓库，因此该示例可以在 Unix 本地运行中以确定性方式进行验证。

## 关键选择

当基础运行正常后，大多数人接下来会关注这些选择：

- `default_manifest`：用于全新 sandbox 会话的文件、仓库、目录和挂载
- `instructions`：应在各个提示词中统一适用的简短工作流规则
- `base_instructions`：一种高级兜底方式，用于替换 SDK 的 sandbox 提示词
- `capabilities`：sandbox 原生工具，例如文件系统编辑/图像检查、shell、skills、memory 和 compaction
- `run_as`：面向模型的工具所使用的 sandbox 用户身份
- `SandboxRunConfig.client`：sandbox 后端
- `SandboxRunConfig.session`、`session_state` 或 `snapshot`：后续运行如何重新连接到先前工作

## 后续内容

- [概念](sandbox/guide.md)：了解 manifest、capabilities、权限、快照、运行配置和组合模式。
- [Sandbox 客户端](sandbox/clients.md)：选择 Unix 本地、Docker、托管提供方以及挂载策略。
- [智能体 memory](sandbox/memory.md)：保留并复用先前 sandbox 运行中的经验。

如果 shell 访问只是偶尔使用的工具之一，请先查看[tools 指南](tools.md)中的托管 shell。若工作区隔离、sandbox 客户端选择或 sandbox 会话恢复行为是设计的一部分，则应使用 sandbox 智能体。