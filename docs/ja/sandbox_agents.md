---
search:
  exclude: true
---
# クイックスタート

最新のエージェントは、ファイルシステム内の実ファイルを操作できる場合に最も効果を発揮します。Agents SDK の **サンドボックスエージェント** は、大規模なドキュメント群の検索、ファイルの編集、コマンドの実行、成果物の生成、保存されたサンドボックス状態からの作業再開が可能な永続ワークスペースをモデルに提供します。

SDK は、ファイルのステージング、ファイルシステムツール、シェルアクセス、サンドボックスのライフサイクル、スナップショット、プロバイダー固有の連携を自身で組み合わせることなく、この実行基盤を提供します。通常の `Agent` と `Runner` のフローを維持したまま、ワークスペース用の `Manifest`、サンドボックスネイティブツール用の機能、作業の実行場所を指定する `SandboxRunConfig` を追加できます。

## 前提条件 {#prerequisites}

- Python 3.10 以降
- OpenAIAgents SDK に関する基本的な知識
- サンドボックスクライアント。信頼できるローカル開発では、`UnixLocalSandboxClient` から始めてください。

## インストール {#installation}

SDK をまだインストールしていない場合:

```bash
pip install openai-agents
```

Docker ベースのサンドボックスの場合:

```bash
pip install "openai-agents[docker]"
```

## ローカルサンドボックスエージェントの作成 {#create-a-local-sandbox-agent}

この例では、ローカルリポジトリを `repo/` 配下にステージングし、ローカルスキルを遅延ロードして、Runner に実行用の Unix ローカルサンドボックスセッションを作成させます。

!!! warning "ローカルコマンドによるホスト権限の使用"

    Linux では、`UnixLocalSandboxClient` はコマンドに OS レベルの制約を追加しません。macOS では、`sandbox-exec` を通じてファイルシステムの制限を適用しますが、ネットワーク分離は提供しません。この例は、信頼できるローカル開発、または外部で分離された環境内で使用してください。信頼できない入力の影響を受けるコマンドを含む、信頼できないコマンドには、適切に構成された Docker またはホスト型サンドボックスを選択するか、外部の分離環境を用意してください。[Unix ローカル実行の制限](sandbox/clients.md#decision-guide)を参照してください。

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

[examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py)を参照してください。この例では、Unix ローカル実行で決定論的に検証できるように、小規模なシェルベースのリポジトリを使用しています。

## 主な選択肢 {#key-choices}

基本的な実行が機能した後、一般的に次に検討する選択肢は次のとおりです。

- `default_manifest`: 新しいサンドボックスセッション用のファイル、リポジトリ、ディレクトリ、マウント
- `instructions`: 複数のプロンプトに共通して適用する短いワークフロールール
- `base_instructions`: SDK のサンドボックスプロンプトを置き換えるための高度なエスケープハッチ
- `capabilities`: ファイルシステムの編集や画像検査、シェル、スキル、メモリ、SDK のコンパクション機構などのサンドボックスネイティブツール
- `run_as`: モデル向けツールを実行するサンドボックスのユーザーアカウント
- `SandboxRunConfig.client`: サンドボックスバックエンド
- `SandboxRunConfig.session`、`session_state`、または `snapshot`: 後続の実行で以前の作業に再接続する方法

## 次のステップ {#where-to-go-next}

- [概念](sandbox/guide.md): マニフェスト、機能、権限、スナップショット、実行設定、構成パターンについて理解します。
- [サンドボックスクライアント](sandbox/clients.md): Unix ローカル、Docker、ホスト型プロバイダー、マウント戦略を選択します。
- [エージェントメモリ](sandbox/memory.md): 以前のサンドボックス実行から得た知見を保存し、再利用します。

シェルアクセスをたまに使用するツールの 1 つとしてのみ必要とする場合は、[ツールガイド](tools.md)のホスト型シェルから始めてください。ワークスペースの分離、サンドボックスクライアントの選択、またはサンドボックスセッションの再開動作が設計の一部である場合は、サンドボックスエージェントを使用してください。