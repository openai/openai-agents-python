---
search:
  exclude: true
---
# 快速入门

!!! warning "Beta 功能"

    沙盒智能体仍处于 Beta 阶段。在正式发布前，API、默认设置和支持的能力细节可能会发生变化；未来也会逐步提供更高级的功能。

现代智能体在能够操作文件系统中的真实文件时效果最佳。Agents SDK 中的**沙盒智能体**为模型提供了一个持久工作区，使其可以检索大型文档集、编辑文件、运行命令、生成产物，并从已保存的沙盒状态继续工作。

SDK 为你提供了这一执行框架，而无需你自行串接文件暂存、文件系统工具、shell 访问、沙盒生命周期、快照以及特定提供商的粘合代码。你可以保留常规的 `Agent` 和 `Runner` 流程，然后为工作区添加 `Manifest`，为沙盒原生工具添加能力，并通过 `SandboxRunConfig` 指定工作运行的位置。

## 前提条件

- Python 3.10 或更高版本
- 基本熟悉 OpenAI Agents SDK
- 一个沙盒客户端。对于本地开发，请从 `UnixLocalSandboxClient` 开始。

## 安装

如果你尚未安装 SDK：

```bash
pip install openai-agents
```

对于由 Docker 支持的沙盒：

```bash
pip install "openai-agents[docker]"
```

## 本地沙盒智能体的创建

此示例会将本地 repo 暂存到 `repo/` 下，惰性加载本地技能，并让运行器为本次运行创建一个 Unix 本地沙盒会话。

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
        build_agent("gpt-5.5"),
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

参见 [examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py)。它使用一个很小的基于 shell 的 repo，因此该示例可以在 Unix 本地运行中以确定性的方式验证。

## 关键选择

在基本运行可用后，大多数人接下来会关注的选择包括：

- `default_manifest`：用于新沙盒会话的文件、repo、目录和挂载
- `instructions`：应跨提示词生效的简短工作流规则
- `base_instructions`：用于替换 SDK 沙盒提示词的高级逃生口
- `capabilities`：沙盒原生工具，例如文件系统编辑/图像检查、shell、技能、内存和压缩
- `run_as`：面向模型的工具所使用的沙盒用户身份
- `SandboxRunConfig.client`：沙盒后端
- `SandboxRunConfig.session`、`session_state` 或 `snapshot`：后续运行如何重新连接到先前的工作

## 后续方向

- [概念](sandbox/guide.md)：了解 manifest、能力、权限、快照、运行配置和组合模式。
- [沙盒客户端](sandbox/clients.md)：选择 Unix 本地、Docker、托管提供商以及挂载策略。
- [智能体内存](sandbox/memory.md)：保留并复用以往沙盒运行中的经验。

如果 shell 访问只是偶尔使用的工具，请从[工具指南](tools.md)中的托管 shell 开始。当工作区隔离、沙盒客户端选择或沙盒会话恢复行为属于设计的一部分时，再选择沙盒智能体。