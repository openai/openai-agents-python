---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、エージェントは別のエージェントにタスクを委任できます。これは、異なるエージェントがそれぞれ別の領域に特化しているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ専門に処理するエージェントを用意できます。

ハンドオフは、LLMに対してツールとして表現されます。そのため、`Refund Agent` という名前のエージェントへのハンドオフがある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成 {#creating-a-handoff}

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接受け取るか、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ることができます。

通常の `Agent` インスタンスを渡すと、その [`handoff_description`][agents.agent.Agent.handoff_description] が設定されている場合、デフォルトのツール説明に追加されます。完全な `handoff()` オブジェクトを作成せずに、モデルがそのハンドオフを選択すべきタイミングを示すヒントとして使用してください。

Agents SDKが提供する [`handoff()`][agents.handoffs.handoff] 関数を使用して、ハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加え、オプションのオーバーライドや入力フィルターを指定できます。

### 基本的な使用法 {#basic-usage}

シンプルなハンドオフは、次のように作成できます。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使用するか（`billing_agent` の場合）、`handoff()` 関数を使用できます。

### `handoff()` 関数によるハンドオフのカスタマイズ {#customizing-handoffs-via-the-handoff-function}

[`handoff()`][agents.handoffs.handoff] 関数を使用すると、さまざまな項目をカスタマイズできます。

-   `agent`: ハンドオフ先のエージェントです。
-   `tool_name_override`: デフォルトでは `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` に解決されます。これはオーバーライドできます。
-   `tool_description_override`: `Handoff.default_tool_description()` から生成されるデフォルトのツール説明をオーバーライドします
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されることが判明した時点で、データ取得などを開始する場合に便利です。この関数はエージェントコンテキストを受け取り、必要に応じて LLMが生成した入力も受け取れます。入力データは `input_type` パラメーターで制御されます。
-   `input_type`: ハンドオフのツール呼び出し引数のスキーマです。設定すると、解析済みのペイロードが `on_handoff` に渡されます。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は以下を参照してください。
-   `is_enabled`: ハンドオフが有効かどうかを指定します。ブール値、またはブール値を返す関数を指定でき、実行時にハンドオフを動的に有効化または無効化できます。
-   `nest_handoff_history`: RunConfig レベルの `nest_handoff_history` 設定に対する、ハンドオフごとのオプションのオーバーライドです。`None` の場合、アクティブな実行設定で定義された値が使用されます。

[`handoff()`][agents.handoffs.handoff] ヘルパーは、渡された特定の `agent` に必ず制御を移します。移行先の候補が複数ある場合は、移行先ごとに 1 つのハンドオフを登録し、その中からモデルに選択させてください。独自のハンドオフコードが呼び出し時に返すエージェントを決定する必要がある場合にのみ、カスタムの [`Handoff`][agents.handoffs.Handoff] を使用してください。

```python
from agents import Agent, handoff, RunContextWrapper

def on_handoff(ctx: RunContextWrapper[None]):
    print("Handoff called")

agent = Agent(name="My agent")

handoff_obj = handoff(
    agent=agent,
    on_handoff=on_handoff,
    tool_name_override="custom_handoff_tool",
    tool_description_override="Custom description",
)
```

## ハンドオフ入力 {#handoff-inputs}

状況によっては、ハンドオフを呼び出す際に LLMからデータを提供させたい場合があります。たとえば、「エスカレーションエージェント」へのハンドオフを考えてみましょう。ログに記録できるよう、モデルに理由を提供させることができます。

```python
from pydantic import BaseModel

from agents import Agent, handoff, RunContextWrapper

class EscalationData(BaseModel):
    reason: str

async def on_handoff(ctx: RunContextWrapper[None], input_data: EscalationData):
    print(f"Escalation agent called with reason: {input_data.reason}")

agent = Agent(name="Escalation agent")

handoff_obj = handoff(
    agent=agent,
    on_handoff=on_handoff,
    input_type=EscalationData,
)
```

`input_type` は、ハンドオフのツール呼び出し自体の引数を記述します。SDK はそのスキーマをハンドオフツールの `parameters` としてモデルに公開し、返された JSON をローカルで検証して、解析済みの値を `on_handoff` に渡します。

`is_enabled` は、モデルがハンドオフ引数を返す前に、SDK が利用可能なハンドオフを準備する際に評価されるため、引数を伴うハンドオフ内の値を認可することはできません。認可が解析済みフィールドに依存する場合は、アプリケーションで副作用が発生する前に、`on_handoff` の先頭でチェックを実行してください。認可に失敗した場合は、値を返すのではなく例外を発生させてください。`on_handoff` が正常に戻ると、SDK は移行を続行します。ツール入力ガードレールは関数ツールに適用され、ハンドオフには適用されません。

これは、次のエージェントのメイン入力を置き換えるものでも、別の移行先を選択するものでもありません。[`handoff()`][agents.handoffs.handoff] ヘルパーは、ラップした特定のエージェントへ引き続き移行します。また、[`input_filter`][agents.handoffs.Handoff.input_filter] またはネストされたハンドオフ履歴設定で変更しない限り、受信側のエージェントには引き続き会話履歴が表示されます。

`input_type` は [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] とも別のものです。`input_type` は、ローカルにすでに存在するアプリケーション状態や依存関係ではなく、ハンドオフ時にモデルが決定するメタデータに使用してください。

### `input_type` の使用場面 {#when-to-use-input_type}

ハンドオフに `reason`、`language`、`priority`、`summary` など、モデルが生成する少量のメタデータが必要な場合は、`input_type` を使用してください。たとえば、トリアージエージェントは `{ "reason": "duplicate_charge", "priority": "high" }` を指定して返金エージェントへハンドオフでき、返金エージェントが引き継ぐ前に `on_handoff` でそのメタデータをログに記録したり永続化したりできます。

目的が異なる場合は、別の仕組みを選択してください。

-   既存のアプリケーション状態と依存関係は、[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] に格納してください。[コンテキストガイド](context.md)を参照してください。
-   受信側のエージェントに表示される履歴を変更する場合は、[`input_filter`][agents.handoffs.Handoff.input_filter]、[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]、または [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を使用してください。
-   専門エージェントの候補が複数ある場合は、移行先ごとに 1 つのハンドオフを登録してください。`input_type` は選択されたハンドオフにメタデータを追加できますが、移行先を振り分けるものではありません。
-   会話を移行せず、ネストされた専門エージェントに構造化入力を渡す場合は、[`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool] の使用を推奨します。[ツール](tools.md#structured-input-for-tool-agents)を参照してください。

## 入力フィルター {#input-filters}

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、それまでの会話履歴全体を参照できるようになります。これを変更するには、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] を介して既存の入力を受け取り、新しい `HandoffInputData` を返す必要がある関数です。

[`HandoffInputData`][agents.handoffs.HandoffInputData] には、以下が含まれます。

-   `input_history`: `Runner.run(...)` が開始される前の入力履歴です。
-   `pre_handoff_items`: ハンドオフが呼び出されたエージェントターンより前に生成された項目です。
-   `new_items`: ハンドオフ呼び出しとハンドオフ出力項目を含む、現在のターン中に生成された項目です。
-   `input_items`: `new_items` の代わりに次のエージェントへ転送するオプションの項目です。セッション履歴では `new_items` をそのまま維持しながら、モデル入力をフィルタリングできます。
-   `run_context`: ハンドオフが呼び出された時点でアクティブだった [`RunContextWrapper`][agents.run_context.RunContextWrapper] です。

ネストされたハンドオフ履歴は、オプトインのベータ機能として利用でき、安定化を進めている間はデフォルトで無効になっています。[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を有効にすると、ランナーは要約可能な履歴を順序付きのアシスタント要約セグメントに圧縮しながら、情報を失わないメッセージ項目を元の位置に保持します。生成される各要約セグメントでは `<CONVERSATION HISTORY>` ラッパーが使用され、後続のハンドオフでは、順序付きのトランスクリプトを再構築する前に、以前に生成されたセグメントがフラット化されます。セッション、`RunState`、`RunResult.to_input_list()` は、この SDK 標準履歴へ移動されたメッセージの正確な出現箇所を追跡し、それらが二重に追加されないようにします。内容が同一でも別個のメッセージは引き続き保持されます。組み込みのセグメンテーションを使用する代わりに、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] で独自のマッピング関数を指定し、次のエージェント向けの入力項目の正確なリストを返すことができます。このオプトインは、ハンドオフの `input_filter` とアクティブな実行の `RunConfig.handoff_input_filter` のいずれも設定されていない場合にのみ適用されます。そのため、このリポジトリ内のコード例を含め、すでにペイロードをカスタマイズしている既存コードは、変更なしで現在の動作を維持します。[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡すと、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] が設定され、単一のハンドオフに対してネスト動作をオーバーライドできます。生成される要約セグメントのラッパーテキストのみを変更する場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください。後の実行でデフォルトのラッパーに戻す必要がある場合は、事前に [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] を呼び出してください。

ハンドオフとアクティブな [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] の両方でフィルターが定義されている場合、その特定のハンドオフでは、ハンドオフごとの [`input_filter`][agents.handoffs.Handoff.input_filter] が優先されます。

!!! note

    ハンドオフは単一の実行内に留まります。入力ガードレールは引き続きチェーン内の最初のエージェントにのみ適用され、出力ガードレールは最終出力を生成するエージェントにのみ適用されます。ワークフロー内の各カスタム関数ツール呼び出しを検査する必要がある場合は、ツールガードレールを使用してください。

一般的なパターン（たとえば、履歴からすべてのツール呼び出しを削除するパターン）がいくつかあり、これらは [`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. `FAQ agent` が呼び出されると、履歴からツール関連の項目がすべて自動的に削除されます。

## 推奨プロンプト {#recommended-prompts}

LLMがハンドオフを正しく理解できるように、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨プレフィックスが用意されています。または、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨情報をプロンプトへ自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```