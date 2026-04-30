---
search:
  exclude: true
---
# クイックスタート

!!! warning "ベータ機能"

    Sandbox エージェントはベータ版です。API、デフォルト、およびサポートされる機能の詳細は一般提供前に変更される可能性があり、今後さらに高度な機能が追加される見込みです。

現代のエージェントは、ファイルシステム上の実際のファイルを操作できるときに最も効果を発揮します。Agents SDK の **Sandbox Agents** は、大規模なドキュメントセットの検索、ファイル編集、コマンド実行、成果物の生成、保存された Sandbox 状態からの作業再開が可能な永続的なワークスペースをモデルに提供します。

SDK は、ファイルステージング、ファイルシステムツール、シェルアクセス、Sandbox ライフサイクル、スナップショット、プロバイダー固有の連携を自分でつなぎ合わせることなく、その実行基盤を提供します。通常の `Agent` と `Runner` のフローを維持したまま、ワークスペース用の `Manifest`、Sandbox ネイティブツール用の機能、作業の実行場所を指定する `SandboxRunConfig` を追加します。

## 前提条件

- Python 3.10 以上
- OpenAI Agents SDK の基本的な知識
- Sandbox クライアント。ローカル開発では、`UnixLocalSandboxClient` から始めてください。

## インストール

SDK をまだインストールしていない場合:

```bash
pip install openai-agents
```

Docker ベースの Sandbox の場合:

```bash
pip install "openai-agents[docker]"
```

## ローカル Sandbox エージェントの作成

この例では、ローカルリポジトリを `repo/` 配下にステージングし、ローカルスキルを遅延読み込みし、Runner が実行用の Unix ローカル Sandbox セッションを作成できるようにします。

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

[examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py) を参照してください。この例では、小さなシェルベースのリポジトリを使用しているため、Unix ローカル実行間で決定論的に検証できます。

## 主な選択肢

基本的な実行が動作したら、多くの人が次に検討する選択肢は次のとおりです。

- `default_manifest`: 新しい Sandbox セッション用のファイル、リポジトリ、ディレクトリ、マウント
- `instructions`: プロンプト全体に適用すべき短いワークフロールール
- `base_instructions`: SDK の Sandbox プロンプトを置き換えるための高度なエスケープハッチ
- `capabilities`: ファイルシステム編集/画像検査、シェル、スキル、メモリ、圧縮などの Sandbox ネイティブツール
- `run_as`: モデル向けツールの Sandbox ユーザー ID
- `SandboxRunConfig.client`: Sandbox バックエンド
- `SandboxRunConfig.session`、`session_state`、または `snapshot`: 後続の実行が以前の作業に再接続する方法

## 次のステップ

- [概念](sandbox/guide.md): マニフェスト、機能、権限、スナップショット、実行設定、構成パターンを理解します。
- [Sandbox クライアント](sandbox/clients.md): Unix ローカル、Docker、ホスト型プロバイダー、マウント戦略を選択します。
- [エージェントメモリ](sandbox/memory.md): 以前の Sandbox 実行から得た教訓を保持し、再利用します。

シェルアクセスが時々使うツールの 1 つにすぎない場合は、[ツールガイド](tools.md) のホスト型シェルから始めてください。ワークスペースの分離、Sandbox クライアントの選択、または Sandbox セッションの再開動作が設計の一部である場合は、Sandbox エージェントを使用してください。