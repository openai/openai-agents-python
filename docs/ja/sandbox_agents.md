---
search:
  exclude: true
---
# クイックスタート

!!! warning "ベータ機能"

    サンドボックスエージェントはベータ版です。一般提供前に API の詳細、デフォルト値、サポートされる機能が変更される可能性があり、今後より高度な機能が追加されることが想定されます。

最新のエージェントは、ファイルシステム上の実際のファイルを操作できるときに最も効果を発揮します。Agents SDK の **サンドボックスエージェント** は、モデルに永続的なワークスペースを提供し、大規模なドキュメントセットの検索、ファイル編集、コマンド実行、成果物の生成、保存済みのサンドボックス状態からの作業再開を可能にします。

SDK は、ファイルのステージング、ファイルシステムツール、シェルアクセス、サンドボックスのライフサイクル、スナップショット、プロバイダー固有の連携を自分で組み合わせる必要なく、その実行ハーネスを提供します。通常の `Agent` と `Runner` のフローはそのままに、ワークスペース用の `Manifest`、サンドボックスネイティブツール用の機能、作業の実行場所を指定する `SandboxRunConfig` を追加します。

## 前提条件

- Python 3.10 以上
- OpenAI Agents SDK に関する基本的な理解
- サンドボックスクライアント。ローカル開発では、`UnixLocalSandboxClient` から始めてください。

## インストール

SDK をまだインストールしていない場合:

```bash
pip install openai-agents
```

Docker ベースのサンドボックスの場合:

```bash
pip install "openai-agents[docker]"
```

## ローカルサンドボックスエージェントの作成

この例では、ローカルリポジトリを `repo/` 配下にステージングし、ローカルスキルを遅延ロードし、ランナーが実行用の Unix ローカルサンドボックスセッションを作成できるようにします。

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

[examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py) を参照してください。この例は小さなシェルベースのリポジトリを使用しているため、Unix ローカル実行間で決定論的に検証できます。

## 主な選択肢

基本的な実行が動作したら、次に多くの人が検討する選択肢は次のとおりです。

- `default_manifest`: 新しいサンドボックスセッション用のファイル、リポジトリ、ディレクトリ、マウント
- `instructions`: 複数のプロンプトにわたって適用すべき短いワークフロールール
- `base_instructions`: SDK のサンドボックスプロンプトを置き換えるための高度なエスケープハッチ
- `capabilities`: ファイルシステム編集 / 画像検査、シェル、スキル、メモリ、コンパクションなどのサンドボックスネイティブツール
- `run_as`: モデル向けツールで使用するサンドボックスのユーザー ID
- `SandboxRunConfig.client`: サンドボックスバックエンド
- `SandboxRunConfig.session`、`session_state`、または `snapshot`: 後続の実行が以前の作業に再接続する方法

## 次のステップ

- [概念](sandbox/guide.md): マニフェスト、機能、権限、スナップショット、実行設定、構成パターンを理解します。
- [サンドボックスクライアント](sandbox/clients.md): Unix ローカル、Docker、ホスト型プロバイダー、マウント戦略を選択します。
- [エージェントメモリ](sandbox/memory.md): 以前のサンドボックス実行から得た知見を保存し、再利用します。

シェルアクセスがたまに使うツールの 1 つにすぎない場合は、[ツールガイド](tools.md) のホスト型シェルから始めてください。ワークスペース分離、サンドボックスクライアントの選択、またはサンドボックスセッションの再開動作が設計に含まれる場合は、サンドボックスエージェントを選択してください。