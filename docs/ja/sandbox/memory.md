---
search:
  exclude: true
---
# エージェントメモリ

メモリにより、今後の sandbox エージェントの実行は過去の実行から学習できます。これは、メッセージ履歴を保存する SDK の会話用 [`Session`](../sessions/index.md) メモリとは別のものです。メモリは、過去の実行から得た学びを sandbox ワークスペース内のファイルに要約します。

!!! warning "ベータ機能"

    Sandbox エージェントはベータ版です。一般提供までに API の詳細、デフォルト値、サポートされる機能が変更される可能性があり、時間とともにさらに高度な機能が追加されることも想定してください。

メモリは、今後の実行における 3 種類のコストを削減できます。

1. エージェントのコスト: エージェントがワークフローの完了に長い時間を要した場合、次回の実行では探索が少なくて済むはずです。これにより、トークン使用量と完了までの時間を削減できます。
2. ユーザーのコスト: ユーザーがエージェントを修正したり好みを表明したりした場合、今後の実行でそのフィードバックを記憶できます。これにより、人による介入を削減できます。
3. コンテキストのコスト: エージェントが以前にタスクを完了していて、ユーザーがそのタスクを発展させたい場合、ユーザーは以前のスレッドを探したり、すべてのコンテキストを再入力したりする必要がないはずです。これにより、タスク説明を短くできます。

バグを修正し、メモリを生成し、スナップショットを再開し、そのメモリを後続の検証実行で使用する、2 回の実行からなる完全なコード例については、[examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py) を参照してください。独立したメモリレイアウトを持つマルチターン、マルチエージェントのコード例については、[examples/sandbox/memory_multi_agent_multiturn.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory_multi_agent_multiturn.py) を参照してください。

## メモリの有効化

sandbox エージェントに機能として `Memory()` を追加します。

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

読み取りが有効な場合、`Memory()` には `Shell()` が必要です。これにより、挿入されたサマリーだけでは不十分なときに、エージェントがメモリファイルを読み取り、検索できます。ライブメモリ更新が有効な場合（デフォルト）、`Filesystem()` も必要です。これにより、エージェントが古くなったメモリを発見した場合や、ユーザーがメモリの更新を依頼した場合に、`memories/MEMORY.md` を更新できます。

デフォルトでは、メモリアーティファクトは sandbox ワークスペースの `memories/` 配下に保存されます。後の実行で再利用するには、同じライブ sandbox セッションを維持するか、永続化されたセッション状態またはスナップショットから再開することで、設定済みのメモリディレクトリ全体を保持して再利用してください。新しい空の sandbox は空のメモリで開始されます。

`Memory()` は、メモリの読み取りと生成の両方を有効にします。メモリを読み取るが新しいメモリは生成すべきでないエージェントには、`Memory(generate=None)` を使用します。たとえば、内部エージェント、サブエージェント、チェッカー、または実行から得られるシグナルが多くない 1 回限りのツールエージェントです。後で使うメモリを生成する必要はあるものの、ユーザーが既存メモリによる影響を望まない場合は、`Memory(read=None)` を使用します。

## メモリの読み取り

メモリ読み取りでは段階的開示を使用します。実行の開始時に、SDK は一般的に役立つヒント、ユーザーの好み、利用可能なメモリの小さなサマリー（`memory_summary.md`）を、エージェントの developer プロンプトに挿入します。これにより、エージェントは過去の作業が関連しそうかどうかを判断するのに十分なコンテキストを得られます。

過去の作業が関連しそうな場合、エージェントは現在のタスクからキーワードを抽出して、設定されたメモリインデックス（`memories_dir` 配下の `MEMORY.md`）を検索します。より詳細が必要な場合にのみ、設定された `rollout_summaries/` ディレクトリ配下にある対応する過去のロールアウトサマリーを開きます。

メモリは古くなることがあります。エージェントには、メモリをガイダンスとしてのみ扱い、現在の環境を信頼するよう指示されています。デフォルトでは、メモリ読み取りでは `live_update` が有効です。そのため、エージェントが古くなったメモリを発見した場合、同じ実行内で設定済みの `MEMORY.md` を更新できます。実行中にメモリを読み取るが変更してほしくない場合、たとえばレイテンシに敏感な実行では、ライブ更新を無効にしてください。

## メモリの生成

実行が完了すると、sandbox ランタイムはその実行セグメントを会話ファイルに追記します。蓄積された会話ファイルは、sandbox セッションが閉じられるときに処理されます。

メモリ生成には 2 つのフェーズがあります。

1. フェーズ 1: 会話の抽出。メモリ生成モデルが、蓄積された 1 つの会話ファイルを処理し、会話サマリーを生成します。system、developer、reasoning のコンテンツは省略されます。会話が長すぎる場合は、先頭と末尾を保持したうえで、コンテキストウィンドウに収まるよう切り詰められます。また、未加工のメモリ抽出も生成します。これは、フェーズ 2 が統合できる会話からの簡潔なメモです。
2. フェーズ 2: レイアウトの統合。統合エージェントは、1 つのメモリレイアウトに対応する未加工のメモリを読み取り、より多くの根拠が必要な場合は会話サマリーを開き、パターンを `MEMORY.md` と `memory_summary.md` に抽出します。

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

`extra_prompt` を使用して、GTM エージェント向けの顧客や会社の詳細など、ユースケースで最も重要なシグナルをメモリ生成器に伝えます。

最近の未加工メモリが `max_raw_memories_for_consolidation`（デフォルトは 256）を超える場合、フェーズ 2 は最新の会話のメモリだけを保持し、古いものを削除します。新しさは、会話が最後に更新された時刻に基づきます。この忘却メカニズムにより、メモリが最新の環境を反映しやすくなります。

## マルチターン会話

マルチターンの sandbox チャットでは、同じライブ sandbox セッションとともに通常の SDK `Session` を使用します。

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

どちらの実行も、同じ SDK 会話セッション（`session=conversation_session`）を渡すため、1 つのメモリ会話ファイルに追記され、したがって同じ `session.session_id` を共有します。これはライブワークスペースを識別する sandbox（`sandbox`）とは異なります。`sandbox` はメモリ会話 ID としては使用されません。sandbox セッションが閉じられると、フェーズ 1 は蓄積された会話を参照するため、2 つの孤立したターンではなく、やり取り全体からメモリを抽出できます。

複数の `Runner.run(...)` 呼び出しを 1 つのメモリ会話にしたい場合は、それらの呼び出し全体で安定した識別子を渡してください。メモリが実行を会話に関連付けるときは、次の順序で解決します。

1. `conversation_id`（`Runner.run(...)` に渡した場合）
2. `session.session_id`（`SQLiteSession` などの SDK `Session` を渡した場合）
3. `RunConfig.group_id`（上記のどちらも存在しない場合）
4. 生成された実行ごとの ID（安定した識別子が存在しない場合）

## エージェントごとのメモリ分離における異なるレイアウトの利用

メモリの分離はエージェント名ではなく `MemoryLayoutConfig` に基づきます。同じレイアウトと同じメモリ会話 ID を持つエージェントは、1 つのメモリ会話と 1 つの統合済みメモリを共有します。異なるレイアウトを持つエージェントは、同じ sandbox ワークスペースを共有している場合でも、別々のロールアウトファイル、未加工メモリ、`MEMORY.md`、`memory_summary.md` を保持します。

複数のエージェントが 1 つの sandbox を共有するものの、メモリは共有すべきでない場合は、別々のレイアウトを使用します。

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