---
search:
  exclude: true
---
# エージェントメモリ

メモリを使用すると、今後のサンドボックスエージェントの実行で過去の実行から学習できます。これは、メッセージ履歴を保存する Agents SDK の会話用 [`Session`](../sessions/index.md) メモリとは別のものです。メモリは、過去の実行から得た知見を抽出し、サンドボックスワークスペース内のファイルに保存します。

!!! warning "ベータ機能"

    サンドボックスエージェントはベータ版です。一般提供の開始までに、API の詳細、デフォルト、対応機能が変更される可能性があります。また、今後さらに高度な機能が追加される予定です。

メモリは、今後の実行にかかる次の 3 種類のコストを削減できます。

1. エージェントのコスト: エージェントがワークフローの完了に長い時間を要した場合、次回の実行では調査を減らせるはずです。これにより、トークン使用量と完了までの時間を削減できます。
2. ユーザーのコスト: ユーザーがエージェントを修正したり、好みを伝えたりした場合、今後の実行でそのフィードバックを記憶できます。これにより、人手による介入を減らせます。
3. コンテキストのコスト: エージェントが以前にタスクを完了しており、ユーザーがそのタスクを基に作業を続けたい場合、以前のスレッドを探したり、すべてのコンテキストを再入力したりする必要がなくなります。これにより、タスクの説明を短くできます。

バグの修正、メモリの生成、スナップショットの再開、そのメモリを使用したフォローアップの検証実行を含む、完全な 2 回実行のコード例については、[examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py) を参照してください。メモリレイアウトを分離したマルチターン、マルチエージェントのコード例については、[examples/sandbox/memory_multi_agent_multiturn.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory_multi_agent_multiturn.py) を参照してください。

## メモリの有効化 {#enable-memory}

サンドボックスエージェントのケイパビリティとして `Memory()` を追加します。

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

読み取りが有効な場合、`Memory()` には `Shell()` が必要です。これにより、注入されたサマリーだけでは不十分なときに、エージェントがメモリファイルを読み取って検索できます。ライブメモリ更新が有効な場合（デフォルト）には、`Filesystem()` も必要です。これにより、エージェントが古くなったメモリを検出した場合や、ユーザーからメモリの更新を依頼された場合に、`memories/MEMORY.md` を更新できます。

デフォルトでは、メモリアーティファクトはサンドボックスワークスペース内の `memories/` 配下に保存されます。後続の実行で再利用するには、同じライブサンドボックスセッションを維持するか、永続化されたセッション状態またはスナップショットから再開し、設定済みのメモリディレクトリ全体を保持して再利用してください。新しい空のサンドボックスでは、メモリも空の状態から開始します。

`Memory()` は、メモリの読み取りと生成の両方を有効にします。メモリを読み取る必要はあるものの、新しいメモリを生成すべきでないエージェントには、`Memory(generate=None)` を使用します。たとえば、内部エージェント、サブエージェント、チェッカー、単発のツールエージェントによる実行では、有用な情報があまり追加されない場合があります。後で使用するメモリを実行で生成する必要はあるものの、既存のメモリがその実行に影響することをユーザーが望まない場合は、`Memory(read=None)` を使用します。

## メモリの読み取り {#read-memory}

メモリの読み取りには段階的開示が使用されます。実行開始時に、SDK は一般的に役立つヒント、ユーザーの好み、利用可能なメモリをまとめた小さなサマリー（`memory_summary.md`）をエージェントの開発者プロンプトに注入します。これにより、エージェントは過去の作業が関連する可能性を判断するのに十分なコンテキストを得られます。

過去の作業が関連していると思われる場合、エージェントは現在のタスクのキーワードを使用して、設定済みのメモリインデックス（`memories_dir` 配下の `MEMORY.md`）を検索します。さらに詳細な情報がタスクに必要な場合に限り、設定済みの `rollout_summaries/` ディレクトリにある、対応する過去のロールアウトサマリーを開きます。

メモリは古くなる可能性があります。エージェントは、メモリをあくまで参考情報として扱い、現在の環境を信頼するよう指示されます。デフォルトでは、メモリの読み取りで `live_update` が有効になっているため、エージェントが古くなったメモリを検出すると、同じ実行内で設定済みの `MEMORY.md` を更新できます。エージェントがメモリを読み取る必要はあるものの、実行中に変更すべきでない場合は、ライブ更新を無効にしてください。たとえば、レイテンシーが重視される実行が該当します。

## メモリの生成 {#generate-memory}

実行が終了すると、サンドボックスランタイムはその実行セグメントを会話ファイルに追記します。蓄積された会話ファイルは、サンドボックスセッションの終了時に処理されます。

メモリ生成には 2 つのフェーズがあります。

1. フェーズ 1: 会話の抽出。メモリ生成モデルが、蓄積された 1 つの会話ファイルを処理し、会話サマリーを生成します。システム、開発者、推論のコンテンツは除外されます。会話が長すぎる場合は、冒頭と末尾を維持しながら、コンテキストウィンドウに収まるよう切り詰められます。また、未加工のメモリ抽出も生成されます。これは、フェーズ 2 で統合できる、会話から得た簡潔なメモです。
2. フェーズ 2: レイアウトの統合。統合エージェントが 1 つのメモリレイアウトにある未加工のメモリを読み取り、さらに根拠が必要な場合は会話サマリーを開き、パターンを抽出して `MEMORY.md` と `memory_summary.md` に格納します。

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

`MemoryGenerateConfig` を使用して、メモリ生成を設定できます。

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

`extra_prompt` を使用すると、ユースケースで最も重要なシグナルをメモリ生成機能に指定できます。たとえば、GTM エージェント向けの顧客や企業の詳細情報などです。

最近の未加工メモリが `max_raw_memories_for_consolidation`（デフォルトは 256）を超えると、フェーズ 2 は最新の会話から得たメモリのみを保持し、それより古いものを削除します。新しさは、会話が最後に更新された時刻に基づきます。この忘却メカニズムにより、メモリに最新の環境を反映しやすくなります。

## マルチターン会話 {#multi-turn-conversations}

マルチターンのサンドボックスチャットでは、通常の SDK `Session` を同じライブサンドボックスセッションと組み合わせて使用します。

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

両方の実行で同じ SDK 会話セッション（`session=conversation_session`）が渡されるため、同じ `session.session_id` が共有されます。その結果、両方の実行が 1 つのメモリ会話ファイルに追記されます。これは、ライブワークスペースを識別し、メモリの会話 ID としては使用されないサンドボックス（`sandbox`）とは異なります。フェーズ 1 はサンドボックスセッションの終了時に蓄積された会話を参照するため、分離された 2 つのターンではなく、やり取り全体からメモリを抽出できます。

複数の `Runner.run(...)` 呼び出しを 1 つのメモリ会話として扱うには、それらの呼び出し全体で安定した識別子を渡します。メモリが実行を会話に関連付ける際は、次の順序で解決します。

1. `conversation_id`（`Runner.run(...)` に渡した場合）
2. `session.session_id`（`SQLiteSession` などの SDK `Session` を渡した場合）
3. `RunConfig.group_id`（上記のいずれも存在しない場合）
4. 安定した識別子が存在しない場合は、実行ごとに生成される ID

## 異なるレイアウトによるエージェントごとのメモリ分離 {#use-different-layouts-to-isolate-memory-for-different-agents}

メモリの分離は、エージェント名ではなく `MemoryLayoutConfig` に基づきます。同じレイアウトと同じメモリ会話 ID を持つエージェントは、1 つのメモリ会話と 1 つの統合済みメモリを共有します。異なるレイアウトを持つエージェントは、同じサンドボックスワークスペースを共有している場合でも、ロールアウトファイル、未加工メモリ、`MEMORY.md`、`memory_summary.md` を個別に保持します。

複数のエージェントが 1 つのサンドボックスを共有していても、メモリは共有すべきでない場合は、別々のレイアウトを使用します。

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

これにより、GTM 分析がエンジニアリングのバグ修正メモリに統合されることも、その逆も防げます。