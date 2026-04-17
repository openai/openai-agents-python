---
search:
  exclude: true
---
# エージェントメモリ

メモリを使うと、今後の sandbox-agent の実行が過去の実行から学習できるようになります。これは、メッセージ履歴を保存する SDK の会話用 [`Session`](../sessions/index.md) メモリとは別のものです。メモリは、過去の実行から得られた学びを sandbox ワークスペース内のファイルに要約します。

!!! warning "ベータ機能"

    Sandbox エージェントはベータ版です。一般提供までに API の詳細、デフォルト設定、サポートされる機能は変更される可能性があり、今後さらに高度な機能も追加される予定です。

メモリは、将来の実行における次の 3 種類のコストを削減できます。

1. エージェントコスト: エージェントがワークフローの完了に長い時間を要した場合、次回の実行では探索が少なくて済むはずです。これにより、トークン使用量と完了までの時間を削減できます。
2. ユーザーコスト: ユーザーがエージェントを修正したり、好みを示したりした場合、今後の実行ではそのフィードバックを記憶できます。これにより、人手による介入を減らせます。
3. コンテキストコスト: エージェントが以前にタスクを完了していて、ユーザーがそのタスクを引き継いで進めたい場合、ユーザーは以前のスレッドを探したり、すべてのコンテキストを再入力したりする必要がありません。これにより、タスクの説明を短くできます。

バグを修正し、メモリを生成し、スナップショットを再開し、そのメモリを後続の verifier 実行で使用する 2 回実行の完全な例については、[examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py) を参照してください。別々のメモリレイアウトを使ったマルチターン・マルチエージェントの例については、[examples/sandbox/memory_multi_agent_multiturn.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory_multi_agent_multiturn.py) を参照してください。

## メモリの有効化

sandbox エージェントの capability として `Memory()` を追加します。

```python
from pathlib import Path
import tempfile

from agents.sandbox import LocalSnapshotSpec, SandboxAgent
from agents.sandbox.capabilities import Filesystem, Memory, Shell

agent = SandboxAgent(
    name="Memory-enabled reviewer",
    instructions="Inspect the workspace and preserve useful lessons for follow-up runs.",
    capabilities=[Memory(), Filesystem(), Shell()],
)

with tempfile.TemporaryDirectory(prefix="sandbox-memory-example-") as snapshot_dir:
    sandbox = await client.create(
        manifest=manifest,
        snapshot=LocalSnapshotSpec(base_path=Path(snapshot_dir)),
    )
```

読み取りが有効な場合、`Memory()` には `Shell()` が必要です。これにより、注入された要約だけでは不十分なときに、エージェントがメモリファイルを読み取り、検索できます。ライブメモリ更新が有効な場合（デフォルト）、`Filesystem()` も必要です。これにより、エージェントが古いメモリを見つけた場合や、ユーザーがメモリの更新を求めた場合に、`memories/MEMORY.md` を更新できます。

デフォルトでは、メモリアーティファクトは sandbox ワークスペースの `memories/` 以下に保存されます。後続の実行でそれらを再利用するには、同じライブ sandbox セッションを維持するか、永続化されたセッション状態またはスナップショットから再開することで、設定された memories ディレクトリー全体を保持して再利用してください。新しい空の sandbox は空のメモリで開始します。

`Memory()` は、メモリの読み取りと生成の両方を有効にします。メモリを読み取るが新しいメモリを生成すべきではないエージェントには `Memory(generate=None)` を使用します。たとえば、内部エージェント、subagent、checker、またはシグナルをあまり追加しない単発のツールエージェントです。実行で後のためにメモリを生成すべきだが、既存のメモリの影響は受けたくない場合は、`Memory(read=None)` を使用します。

## メモリの読み取り

メモリの読み取りでは段階的開示を使用します。実行開始時に、SDK は一般的に有用なヒント、ユーザーの好み、利用可能なメモリの小さな要約（`memory_summary.md`）をエージェントの開発者プロンプトに注入します。これにより、過去の作業が関連しそうかどうかをエージェントが判断するための十分なコンテキストが与えられます。

過去の作業が関連していそうな場合、エージェントは現在のタスクのキーワードを使って、設定されたメモリインデックス（`memories_dir` 配下の `MEMORY.md`）を検索します。さらに詳しい情報が必要な場合にのみ、設定された `rollout_summaries/` ディレクトリー配下の対応する過去の rollout 要約を開きます。

メモリは古くなることがあります。エージェントには、メモリはあくまで参考情報として扱い、現在の環境を信頼するよう指示されています。デフォルトでは、メモリ読み取りでは `live_update` が有効になっているため、エージェントが古いメモリを見つけた場合、同じ実行内で設定された `MEMORY.md` を更新できます。たとえば、その実行がレイテンシーに敏感な場合など、エージェントがメモリを読み取るだけで実行中に変更すべきでない場合は、ライブ更新を無効にしてください。

## メモリの生成

実行が終了すると、sandbox ランタイムはその実行セグメントを会話ファイルに追記します。蓄積された会話ファイルは、sandbox セッションが閉じられるときに処理されます。

メモリ生成には 2 つのフェーズがあります。

1. フェーズ 1: 会話抽出。メモリ生成モデルが蓄積された 1 つの会話ファイルを処理し、会話要約を生成します。system、developer、および reasoning の内容は省略されます。会話が長すぎる場合は、先頭と末尾を保持したまま、コンテキストウィンドウに収まるように切り詰められます。また、フェーズ 2 で統合できるよう、会話からの簡潔なメモである raw メモリ抽出も生成されます。
2. フェーズ 2: レイアウト統合。統合エージェントが 1 つのメモリレイアウトの raw メモリを読み取り、さらに証拠が必要な場合は会話要約を開き、パターンを `MEMORY.md` と `memory_summary.md` に抽出します。

デフォルトのワークスペースレイアウトは次のとおりです。

```text
workspace/
├── sessions/
│   └── <rollout-id>.jsonl
└── memories/
    ├── memory_summary.md
    ├── MEMORY.md
    ├── raw_memories.md (intermediate)
    ├── phase_two_selection.json (intermediate)
    ├── raw_memories/ (intermediate)
    │   └── <rollout-id>.md
    ├── rollout_summaries/
    │   └── <rollout-id>_<slug>.md
    └── skills/
```

`MemoryGenerateConfig` を使ってメモリ生成を設定できます。

```python
from agents.sandbox import MemoryGenerateConfig
from agents.sandbox.capabilities import Memory

memory = Memory(
    generate=MemoryGenerateConfig(
        max_raw_memories_for_consolidation=128,
        extra_prompt="Pay extra attention to what made the customer more satisfied or annoyed",
    ),
)
```

`extra_prompt` を使うと、GTM エージェント向けの顧客情報や企業情報のように、どのシグナルがユースケースで最も重要かをメモリ生成器に伝えられます。

最近の raw メモリが `max_raw_memories_for_consolidation`（デフォルトは 256）を超える場合、フェーズ 2 は最新の会話のメモリだけを保持し、古いものを削除します。新しさは、その会話が最後に更新された時刻に基づきます。この忘却メカニズムにより、メモリは最新の環境を反映しやすくなります。

## マルチターン会話

マルチターンの sandbox チャットでは、通常の SDK `Session` を同じライブ sandbox セッションと組み合わせて使用します。

```python
from agents import Runner, SQLiteSession
from agents.run import RunConfig
from agents.sandbox import SandboxRunConfig

conversation_session = SQLiteSession("gtm-q2-pipeline-review")
sandbox = await client.create(manifest=agent.default_manifest)

async with sandbox:
    run_config = RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
        workflow_name="GTM memory example",
    )
    await Runner.run(
        agent,
        "Analyze data/leads.csv and identify one promising GTM segment.",
        session=conversation_session,
        run_config=run_config,
    )
    await Runner.run(
        agent,
        "Using that analysis, write a short outreach hypothesis.",
        session=conversation_session,
        run_config=run_config,
    )
```

両方の実行は同じメモリ会話ファイルに追記されます。これは、同じ SDK 会話セッション（`session=conversation_session`）を渡すことで、同じ `session.session_id` を共有するためです。これは、ライブワークスペースを識別する sandbox（`sandbox`）とは異なり、メモリ会話 ID としては使用されません。フェーズ 1 は sandbox セッションが閉じられたときに蓄積された会話を参照するため、分離された 2 つのターンではなく、やり取り全体からメモリを抽出できます。

複数の `Runner.run(...)` 呼び出しを 1 つのメモリ会話にしたい場合は、それらの呼び出しにまたがって安定した識別子を渡してください。メモリが実行を会話に関連付けるときは、次の順序で解決されます。

1. `Runner.run(...)` に渡した `conversation_id`
2. `SQLiteSession` などの SDK `Session` を渡した場合の `session.session_id`
3. 上記のいずれも存在しない場合の `RunConfig.group_id`
4. 安定した識別子が存在しない場合の、実行ごとに生成される ID

## 異なるエージェント向けのメモリ分離用レイアウト

メモリの分離は、エージェント名ではなく `MemoryLayoutConfig` に基づきます。同じレイアウトと同じメモリ会話 ID を持つエージェントは、1 つのメモリ会話と 1 つの統合メモリを共有します。異なるレイアウトを持つエージェントは、同じ sandbox ワークスペースを共有していても、別々の rollout ファイル、raw メモリ、`MEMORY.md`、および `memory_summary.md` を保持します。

複数のエージェントが 1 つの sandbox を共有しているが、メモリを共有すべきでない場合は、別々のレイアウトを使用します。

```python
from agents import SQLiteSession
from agents.sandbox import MemoryLayoutConfig, SandboxAgent
from agents.sandbox.capabilities import Filesystem, Memory, Shell

gtm_agent = SandboxAgent(
    name="GTM reviewer",
    instructions="Analyze GTM workspace data and write concise recommendations.",
    capabilities=[
        Memory(
            layout=MemoryLayoutConfig(
                memories_dir="memories/gtm",
                sessions_dir="sessions/gtm",
            )
        ),
        Filesystem(),
        Shell(),
    ],
)

engineering_agent = SandboxAgent(
    name="Engineering reviewer",
    instructions="Inspect engineering workspaces and summarize fixes and risks.",
    capabilities=[
        Memory(
            layout=MemoryLayoutConfig(
                memories_dir="memories/engineering",
                sessions_dir="sessions/engineering",
            )
        ),
        Filesystem(),
        Shell(),
    ],
)

gtm_session = SQLiteSession("gtm-q2-pipeline-review")
engineering_session = SQLiteSession("eng-invoice-test-fix")
```

これにより、GTM 分析がエンジニアリングのバグ修正メモリに統合されたり、その逆が起きたりすることを防げます。