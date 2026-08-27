---
search:
  exclude: true
---
# クイックスタート

!!! warning "ベータ機能"

    サンドボックスエージェントはベータ版です。一般提供までに API の詳細、デフォルト、サポートされる機能が変更される可能性があります。また、今後さらに高度な機能が追加される予定です。

最新のエージェントは、ファイルシステム上の実際のファイルを操作できる場合に最も効果的に機能します。Agents SDK の **サンドボックスエージェント** は、大規模なドキュメント群の検索、ファイルの編集、コマンドの実行、成果物の生成、保存されたサンドボックス状態からの作業再開が可能な永続的ワークスペースをモデルに提供します。

SDK は、ファイルのステージング、ファイルシステムツール、シェルアクセス、サンドボックスのライフサイクル、スナップショット、プロバイダー固有の連携を自分で組み合わせることなく、この実行基盤を提供します。通常の `Agent` と `Runner` のフローを維持したまま、ワークスペース用の `Manifest`、サンドボックスネイティブツールの機能、作業の実行場所を指定する `SandboxRunConfig` を追加します。

## 前提条件 {#prerequisites}

- Python 3.10 以降
- OpenAI Agents SDK に関する基本的な知識
- サンドボックスクライアント。ローカル開発では、まず `UnixLocalSandboxClient` を使用します。

## インストール {#installation}

SDK をまだインストールしていない場合：

```bash
pip install openai-agents
```

Docker ベースのサンドボックスの場合：

```bash
pip install "openai-agents[docker]"
```

## ローカルサンドボックスエージェントの作成 {#create-a-local-sandbox-agent}

この例では、`repo/` 配下にローカルリポジトリをステージングし、ローカルスキルを遅延読み込みして、実行時にランナーが Unix ローカルのサンドボックスセッションを作成します。

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

[examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py) を参照してください。このコード例では、シェルベースの小さなリポジトリを使用しているため、Unix ローカルでの実行全体にわたって決定論的に検証できます。

## 主な選択肢 {#key-choices}

基本的な実行が機能した後、多くの方が次に検討する選択肢は以下のとおりです。

- `default_manifest`：新しいサンドボックスセッションで使用するファイル、リポジトリ、ディレクトリ、マウント
- `instructions`：複数のプロンプトにわたって適用する短いワークフロールール
- `base_instructions`：SDK のサンドボックスプロンプトを置き換えるための高度なエスケープハッチ
- `capabilities`：ファイルシステムの編集／画像検査、シェル、スキル、メモリ、SDK の圧縮メカニズムなどのサンドボックスネイティブツール
- `run_as`：モデル向けツールの実行に使用されるサンドボックスのユーザーアカウント
- `SandboxRunConfig.client`：サンドボックスのバックエンド
- `SandboxRunConfig.session`、`session_state`、または `snapshot`：後続の実行を以前の作業に再接続する方法

## 次のステップ {#where-to-go-next}

- [概念](sandbox/guide.md)：マニフェスト、機能、権限、スナップショット、実行設定、構成パターンについて説明します。
- [サンドボックスクライアント](sandbox/clients.md)：Unix ローカル、Docker、ホステッドプロバイダー、マウント戦略を選択します。
- [エージェントメモリ](sandbox/memory.md)：以前のサンドボックス実行から得た知見を保持し、再利用します。

シェルアクセスをときどき使用するツールの 1 つとしてのみ必要とする場合は、[ツールガイド](tools.md)のホステッドシェルから始めてください。ワークスペースの分離、サンドボックスクライアントの選択、サンドボックスセッションの再開動作が設計の一部となる場合は、サンドボックスエージェントを使用してください。