---
search:
  exclude: true
---
# クイックスタート

!!! warning "ベータ機能"

    サンドボックスエージェントはベータ版です。一般提供までに API の詳細、デフォルト、サポートされる機能が変更される可能性があります。また、今後さらに高度な機能が追加される予定です。

最新のエージェントは、ファイルシステム上の実ファイルを操作できるときに最大限の力を発揮します。Agents SDK の **サンドボックスエージェント** は、大規模なドキュメント群の検索、ファイルの編集、コマンドの実行、成果物の生成、保存済みのサンドボックス状態からの作業再開が可能な永続ワークスペースをモデルに提供します。

SDK は、ファイルのステージング、ファイルシステムツール、シェルアクセス、サンドボックスのライフサイクル、スナップショット、プロバイダー固有の連携コードを自分で組み合わせることなく、この実行基盤を提供します。通常の `Agent` と `Runner` のフローを維持しながら、ワークスペース用の `Manifest`、サンドボックスネイティブツール用の機能、作業の実行場所を指定する `SandboxRunConfig` を追加できます。

## 前提条件

- Python 3.10 以降
- OpenAI Agents SDK に関する基本的な知識
- サンドボックスクライアント。ローカル開発では、まず `UnixLocalSandboxClient` を使用してください。

## インストール

SDK をまだインストールしていない場合は、次を実行します。

```bash
pip install openai-agents
```

Docker ベースのサンドボックスの場合は、次を実行します。

```bash
pip install "openai-agents[docker]"
```

## ローカルサンドボックスエージェントの作成

この例では、ローカルリポジトリを `repo/` 配下にステージングし、ローカルスキルを遅延読み込みして、実行時にランナーが Unix ローカルのサンドボックスセッションを作成できるようにします。

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

[examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py) を参照してください。この例では、Unix ローカルでの実行間で決定論的に検証できるように、シェルベースの小規模なリポジトリを使用しています。

## 主な選択肢

基本的な実行が動作した後、多くの場合、次の選択肢を検討します。

- `default_manifest`: 新規サンドボックスセッション向けのファイル、リポジトリ、ディレクトリ、マウント
- `instructions`: 複数のプロンプトにわたって適用する短いワークフロールール
- `base_instructions`: SDK のサンドボックスプロンプトを置き換えるための高度な回避手段
- `capabilities`: ファイルシステムの編集／画像検査、シェル、スキル、メモリ、コンパクションなどのサンドボックスネイティブツール
- `run_as`: モデル向けツールで使用するサンドボックスのユーザー ID
- `SandboxRunConfig.client`: サンドボックスのバックエンド
- `SandboxRunConfig.session`、`session_state`、または `snapshot`: 後続の実行を以前の作業へ再接続する方法

## 次のステップ

- [概念](sandbox/guide.md): マニフェスト、機能、権限、スナップショット、実行設定、構成パターンについて理解します。
- [サンドボックスクライアント](sandbox/clients.md): Unix ローカル、Docker、ホステッドプロバイダー、マウント戦略を選択します。
- [エージェントメモリ](sandbox/memory.md): 過去のサンドボックス実行から得た知見を保持し、再利用します。

シェルアクセスを一時的なツールとしてのみ使用する場合は、[ツールガイド](tools.md)のホステッドシェルから始めてください。ワークスペースの分離、サンドボックスクライアントの選択、またはサンドボックスセッションの再開動作が設計に含まれる場合は、サンドボックスエージェントを使用してください。