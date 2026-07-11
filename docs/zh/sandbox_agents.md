---
search:
  exclude: true
---
# 快速入门

!!! warning "测试版功能"

    沙箱智能体目前处于测试阶段。在正式发布之前，API 细节、默认值和支持的功能可能会发生变化，并且未来将逐步提供更高级的功能。

现代智能体能够在文件系统中操作真实文件时，往往能发挥最佳效果。Agents SDK 中的**沙箱智能体**为模型提供持久化工作区，使其能够检索大型文档集、编辑文件、运行命令、生成产物，并从已保存的沙箱状态继续工作。

SDK 为你提供这一执行框架，无需自行整合文件暂存、文件系统工具、shell 访问、沙箱生命周期、快照以及特定于提供商的适配代码。你可以继续使用常规的 `Agent` 和 `Runner` 流程，然后为工作区添加 `Manifest`，为沙箱原生工具添加 capabilities，并使用 `SandboxRunConfig` 指定工作运行的位置。

## 前置条件

- Python 3.10 或更高版本
- 基本熟悉 OpenAI Agents SDK
- 沙箱客户端。对于本地开发，请从 `UnixLocalSandboxClient` 开始。

## 安装

如果尚未安装 SDK：

```bash
pip install openai-agents
```

对于由 Docker 支持的沙箱：

```bash
pip install "openai-agents[docker]"
```

## 本地沙箱智能体的创建

此代码示例将本地仓库存放到 `repo/` 下，延迟加载本地技能，并允许运行器为本次运行创建 Unix 本地沙箱会话。

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

请参阅 [examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py)。它使用一个基于 shell 的小型仓库，因此可以在多次 Unix 本地运行中以确定性方式验证该代码示例。

## 关键选项

基本运行正常后，大多数人接下来会使用以下选项：

- `default_manifest`：全新沙箱会话所使用的文件、仓库、目录和挂载项
- `instructions`：应适用于所有提示词的简短工作流规则
- `base_instructions`：用于替换 SDK 沙箱提示词的高级应急选项
- `capabilities`：沙箱原生工具，例如文件系统编辑/图像检查、shell、技能、记忆和压缩
- `run_as`：面向模型的工具所使用的沙箱用户身份
- `SandboxRunConfig.client`：沙箱后端
- `SandboxRunConfig.session`、`session_state` 或 `snapshot`：后续运行如何重新连接到先前的工作

## 后续步骤

- [概念](sandbox/guide.md)：了解清单、capabilities、权限、快照、运行配置和组合模式。
- [沙箱客户端](sandbox/clients.md)：选择 Unix 本地、Docker、托管提供商和挂载策略。
- [智能体记忆](sandbox/memory.md)：保留并复用以往沙箱运行中获得的经验。

如果 shell 访问只是偶尔使用的工具，请先从[工具指南](tools.md)中的托管 shell 开始。如果工作区隔离、沙箱客户端选择或沙箱会话恢复行为属于设计的一部分，请使用沙箱智能体。