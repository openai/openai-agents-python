---
search:
  exclude: true
---
# エージェントメモリ

メモリにより、今後の sandbox-agent 実行は過去の実行から学習できます。これは、メッセージ履歴を保存する SDK の会話用 [`Session`](../sessions/index.md) メモリとは別物です。メモリは、過去の実行から得た学びを sandbox ワークスペース内のファイルに要約します。

!!! warning "ベータ機能"

    Sandbox エージェントはベータ版です。一般提供前に API の詳細、デフォルト値、対応機能は変更される可能性があり、今後さらに高度な機能が追加される予定です。

メモリは、今後の実行における 3 種類のコストを削減できます。

1. エージェントコスト: エージェントがワークフロー完了に長時間かかった場合、次回実行では探索が少なくて済むはずです。これにより、トークン使用量と完了までの時間を削減できます。
2. ユーザーコスト: ユーザーがエージェントを修正したり好みを示したりした場合、今後の実行ではそのフィードバックを記憶できます。これにより、人手介入を減らせます。
3. コンテキストコスト: エージェントが以前にタスクを完了しており、ユーザーがそのタスクを発展させたい場合、ユーザーは以前のスレッドを探したり、すべてのコンテキストを再入力したりする必要がなくなります。これにより、タスク記述を短くできます。

バグ修正、メモリ生成、スナップショット再開、そしてフォローアップの検証実行でのそのメモリ利用までを含む完全な 2 回実行の例は、[examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py) を参照してください。別々のメモリレイアウトを使うマルチターン・マルチエージェントの例は、[examples/sandbox/memory_multi_agent_multiturn.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory_multi_agent_multiturn.py) を参照してください。

## メモリ有効化

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

読み取りが有効な場合、`Memory()` には `Shell()` が必要です。これにより、注入された要約だけでは不十分なときに、エージェントがメモリファイルを読み取り・検索できます。ライブメモリ更新が有効な場合（デフォルト）、`Filesystem()` も必要です。これにより、エージェントが古いメモリを見つけた場合や、ユーザーがメモリ更新を求めた場合に、`memories/MEMORY.md` を更新できます。

デフォルトでは、メモリアーティファクトは sandbox ワークスペースの `memories/` 配下に保存されます。後続実行で再利用するには、同じライブ sandbox セッションを維持するか、永続化されたセッション状態またはスナップショットから再開して、設定済みの memories ディレクトリー全体を保持・再利用してください。空の新規 sandbox は空のメモリで開始されます。

`Memory()` はメモリの読み取りと生成の両方を有効にします。メモリを読むが新規メモリを生成すべきでないエージェント（例: internal エージェント、subagent、checker、または実行であまり有用なシグナルを追加しない one-off ツールエージェント）には、`Memory(generate=None)` を使用します。実行で後のためにメモリを生成したいが、既存メモリの影響は受けたくない場合は、`Memory(read=None)` を使用します。

## メモリ読み取り

メモリ読み取りは段階的開示を使用します。実行開始時、SDK は一般的に有用なヒント、ユーザーの好み、利用可能なメモリを含む小さな要約（`memory_summary.md`）をエージェントの developer prompt に注入します。これにより、過去の作業が関連しそうかどうかを判断するための十分なコンテキストをエージェントに与えます。

過去の作業が関連すると見なされた場合、エージェントは現在のタスクのキーワードで、設定済みメモリインデックス（`memories_dir` 配下の `MEMORY.md`）を検索します。タスクでより詳細が必要なときにのみ、設定済み `rollout_summaries/` ディレクトリー配下の対応する過去の rollout 要約を開きます。

メモリは古くなる可能性があります。エージェントには、メモリをあくまでガイダンスとして扱い、現在の環境を信頼するよう指示されます。デフォルトでは、メモリ読み取りで `live_update` が有効なため、エージェントが古いメモリを見つけた場合は同一実行内で設定済み `MEMORY.md` を更新できます。実行中にメモリを変更すべきでない場合（例: レイテンシーに敏感な実行）は、ライブ更新を無効にしてください。

## メモリ生成

実行終了後、sandbox ランタイムはその実行セグメントを会話ファイルに追記します。蓄積された会話ファイルは、sandbox セッション終了時に処理されます。

メモリ生成には 2 つのフェーズがあります。

1. フェーズ 1: 会話抽出。メモリ生成モデルが 1 つの蓄積会話ファイルを処理し、会話要約を生成します。system、developer、reasoning コンテンツは除外されます。会話が長すぎる場合は、先頭と末尾を保持したまま、コンテキストウィンドウに収まるよう切り詰められます。また、フェーズ 2 で統合可能な会話由来のコンパクトなノートとして、raw メモリ抽出も生成されます。
2. フェーズ 2: レイアウト統合。統合エージェントが 1 つのメモリレイアウトに対する raw メモリを読み取り、追加の証拠が必要な場合に会話要約を開き、`MEMORY.md` と `memory_summary.md` にパターンを抽出します。

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

`MemoryGenerateConfig` でメモリ生成を設定できます。

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

`extra_prompt` を使うと、GTM エージェント向けの顧客情報や会社情報のように、ユースケースで最も重要なシグナルをメモリ生成器に指定できます。

最近の raw メモリが `max_raw_memories_for_consolidation`（デフォルトは 256）を超える場合、フェーズ 2 は最新の会話のメモリのみを保持し、古いものを削除します。新しさは会話の最終更新時刻に基づきます。この忘却メカニズムにより、メモリは最新の環境を反映しやすくなります。

## マルチターン会話

マルチターン sandbox チャットでは、通常の SDK `Session` を同じライブ sandbox セッションと一緒に使用します。

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

両方の実行は、同じ SDK 会話セッション（`session=conversation_session`）を渡すため、1 つのメモリ会話ファイルに追記されます。したがって同じ `session.session_id` を共有します。これはライブワークスペースを識別する sandbox（`sandbox`）とは異なり、メモリ会話 ID としては使われません。sandbox セッション終了時に、フェーズ 1 は蓄積された会話を参照するため、分離された 2 ターンではなく、やり取り全体からメモリを抽出できます。

複数の `Runner.run(...)` 呼び出しを 1 つのメモリ会話にしたい場合は、それらの呼び出しで安定した識別子を渡してください。メモリが実行を会話に関連付ける際の解決順は次のとおりです。

1. `Runner.run(...)` に渡した `conversation_id`
2. `SQLiteSession` などの SDK `Session` を渡した場合の `session.session_id`
3. 上記のいずれもない場合の `RunConfig.group_id`
4. 安定した識別子がない場合の、実行ごとに生成される ID

## 異なるレイアウトでのエージェント別メモリ分離

メモリ分離はエージェント名ではなく `MemoryLayoutConfig` に基づきます。同じレイアウトと同じメモリ会話 ID を持つエージェントは、1 つのメモリ会話と 1 つの統合メモリを共有します。異なるレイアウトのエージェントは、同じ sandbox ワークスペースを共有していても、rollout ファイル、raw メモリ、`MEMORY.md`、`memory_summary.md` を分離して保持します。

複数のエージェントが 1 つの sandbox を共有しつつ、メモリは共有すべきでない場合は、別々のレイアウトを使用します。

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