---
search:
  exclude: true
---
# クイックスタート

!!! warning "ベータ機能"

    Sandbox Agents はベータ版です。一般提供までの間に API の詳細、デフォルト値、対応機能は変更される可能性があり、また時間の経過とともにより高度な機能が追加される予定です。

現代的なエージェントは、ファイルシステム内の実際のファイルを操作できるときに最も効果的に動作します。Agents SDK の **Sandbox Agents** は、モデルに永続的なワークスペースを提供し、そこでは大規模なドキュメント群の検索、ファイル編集、コマンド実行、成果物の生成、保存された sandbox 状態からの作業再開が可能です。

SDK は、ファイルのステージング、ファイルシステムツール、シェルアクセス、sandbox のライフサイクル、スナップショット、プロバイダー固有の接続処理を自分で組み合わせることなく、その実行ハーネスを提供します。通常の `Agent` と `Runner` のフローはそのまま維持しつつ、ワークスペース用の `Manifest` 、 sandbox ネイティブツール用の capabilities 、そして作業の実行場所を指定する `SandboxRunConfig` を追加できます。

## 前提条件

- Python 3.10 以上
- OpenAI Agents SDK の基本的な知識
- sandbox クライアント。ローカル開発では、まず `UnixLocalSandboxClient` から始めてください。

## インストール

まだ SDK をインストールしていない場合は、次を実行してください。

```bash
pip install openai-agents
```

Docker ベースの sandbox の場合:

```bash
pip install "openai-agents[docker]"
```

## ローカル sandbox エージェントの作成

この例では、ローカルのリポジトリを `repo/` 配下にステージングし、ローカル skills を遅延読み込みし、 runner が実行時に Unix ローカル sandbox セッションを作成できるようにします。

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

[examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py) を参照してください。この例では小さなシェルベースのリポジトリを使用しているため、Unix ローカル実行全体で決定論的に検証できます。

## 主な選択肢

基本的な実行が動作したら、次に多くの人が選ぶ項目は以下です。

- `default_manifest`: 新しい sandbox セッション用のファイル、リポジトリ、ディレクトリ、マウント
- `instructions`: プロンプト全体に適用される短いワークフロールール
- `base_instructions`: SDK の sandbox プロンプトを置き換えるための高度なエスケープハッチ
- `capabilities`: ファイルシステム編集 / 画像検査、シェル、 skills 、メモリ、 compaction などの sandbox ネイティブツール
- `run_as`: モデル向けツールにおける sandbox ユーザー ID
- `SandboxRunConfig.client`: sandbox バックエンド
- `SandboxRunConfig.session` 、 `session_state` 、または `snapshot`: 後続の実行を以前の作業に再接続する方法

## 次の参照先

- [概念](sandbox/guide.md): manifests 、 capabilities 、 permissions 、 snapshots 、 run config 、構成パターンを理解します。
- [Sandbox クライアント](sandbox/clients.md): Unix ローカル、 Docker 、ホスト型プロバイダー、マウント戦略を選びます。
- [エージェントメモリ](sandbox/memory.md): 以前の sandbox 実行から得た学びを保持し、再利用します。

シェルアクセスが時々使うツールの 1 つにすぎない場合は、[ツールガイド](tools.md) のホスト型シェルから始めてください。ワークスペースの分離、sandbox クライアントの選択、または sandbox セッションの再開動作が設計の一部である場合は、sandbox エージェントを使用してください。