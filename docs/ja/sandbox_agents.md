---
search:
  exclude: true
---
# クイックスタート

!!! warning "ベータ機能"

    サンドボックスエージェントは ベータ版 です。一般提供前に API の詳細、デフォルト値、対応機能は変更される可能性があり、時間とともにより高度な機能が追加される予定です。

モダンなエージェントは、ファイルシステム上の実際のファイルを操作できると最も効果的に動作します。Agents SDK の **Sandbox Agents** は、モデルに永続的なワークスペースを提供し、そこでは大規模なドキュメントセットの検索、ファイル編集、コマンド実行、成果物の生成、保存されたサンドボックス状態からの作業再開ができます。

SDK は、ファイルのステージング、ファイルシステムツール、シェルアクセス、サンドボックスのライフサイクル、スナップショット、プロバイダー固有の接続処理を自分で組み合わせることなく、その実行ハーネスを提供します。通常の `Agent` と `Runner` のフローはそのままに、ワークスペース用の `Manifest`、サンドボックスネイティブツール用の capabilities、実行場所を指定する `SandboxRunConfig` を追加します。

## 前提条件

- Python 3.10 以上
- OpenAI Agents SDK の基本的な知識
- サンドボックスクライアント。ローカル開発では、まず `UnixLocalSandboxClient` を使用してください。

## インストール

まだ SDK をインストールしていない場合:

```bash
pip install openai-agents
```

Docker バックエンドのサンドボックスの場合:

```bash
pip install "openai-agents[docker]"
```

## ローカルサンドボックスエージェントの作成

この例では、`repo/` 配下にローカルリポジトリをステージングし、ローカルスキルを遅延読み込みし、ランナーが実行時に Unix ローカルのサンドボックスセッションを作成できるようにします。

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

[examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py) を参照してください。これは小さなシェルベースのリポジトリを使用しており、Unix ローカル実行全体で決定論的に例を検証できます。

## 主要な選択肢

基本的な実行が動作したら、次に多くの人が選ぶのは以下です。

- `default_manifest`: 新しいサンドボックスセッション用のファイル、リポジトリ、ディレクトリ、マウント
- `instructions`: プロンプト全体に適用する短いワークフロールール
- `base_instructions`: SDK のサンドボックスプロンプトを置き換えるための高度なエスケープハッチ
- `capabilities`: ファイルシステム編集 / 画像検査、シェル、スキル、メモリ、コンパクションなどのサンドボックスネイティブツール
- `run_as`: モデル向けツールにおけるサンドボックスユーザー ID
- `SandboxRunConfig.client`: サンドボックスバックエンド
- `SandboxRunConfig.session`、`session_state`、または `snapshot`: 後続の実行で以前の作業に再接続する方法

## 次のステップ

- [概念](sandbox/guide.md): マニフェスト、capabilities、権限、スナップショット、実行設定、構成パターンを理解します。
- [サンドボックスクライアント](sandbox/clients.md): Unix ローカル、Docker、ホスト型プロバイダー、マウント戦略を選択します。
- [エージェントメモリ](sandbox/memory.md): 以前のサンドボックス実行からの学びを保持し再利用します。

シェルアクセスが時々使う 1 つのツールに過ぎない場合は、[ツールガイド](tools.md) のホスト型シェルから始めてください。ワークスペース分離、サンドボックスクライアントの選択、またはサンドボックスセッション再開の挙動が設計の一部である場合は、サンドボックスエージェントを使用してください。