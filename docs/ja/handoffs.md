---
search:
  exclude: true
---
# ハンドオフ

ハンドオフにより、エージェントはタスクを別のエージェントに委任できます。これは、異なるエージェントがそれぞれ別の領域を専門とするシナリオで特に役立ちます。たとえば、カスタマーサポートアプリには、注文ステータス、返金、 FAQ などのタスクをそれぞれ専門に扱うエージェントがあるかもしれません。

ハンドオフは LLM に対してツールとして表現されます。そのため、 `Refund Agent` という名前のエージェントへのハンドオフがある場合、そのツールは `transfer_to_refund_agent` と呼ばれます。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、 `Agent` を直接受け取ることも、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ることもできます。

通常の `Agent` インスタンスを渡す場合、その [`handoff_description`][agents.agent.Agent.handoff_description] （設定されている場合）がデフォルトのツール説明に追加されます。完全な `handoff()` オブジェクトを書かずに、そのハンドオフをモデルが選ぶべきタイミングを示唆するために使用してください。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使用してハンドオフを作成できます。この関数では、必要に応じた上書きや入力フィルターとともに、引き渡し先のエージェントを指定できます。

### 基本的な使用法

シンプルなハンドオフを作成する方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使用することも（ `billing_agent` のように）、 `handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使用すると、さまざまな項目をカスタマイズできます。

-   `agent`: 処理を引き渡す先のエージェントです。
-   `tool_name_override`: デフォルトでは `Handoff.default_tool_name()` 関数が使用され、 `transfer_to_<agent_name>` に解決されます。これは上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` から得られるデフォルトのツール説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されることが分かった時点ですぐにデータ取得を開始する、といった用途に便利です。この関数はエージェントコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターによって制御されます。
-   `input_type`: ハンドオフツール呼び出し引数のスキーマです。設定されている場合、解析されたペイロードが `on_handoff` に渡されます。
-   `input_filter`: これにより、次のエージェントが受け取る入力をフィルタリングできます。詳細は以下を参照してください。
-   `is_enabled`: ハンドオフが有効かどうかです。これはブール値、またはブール値を返す関数にでき、実行時にハンドオフを動的に有効化または無効化できます。
-   `nest_handoff_history`: RunConfig レベルの `nest_handoff_history` 設定に対する、呼び出しごとの任意の上書きです。 `None` の場合は、アクティブな実行設定で定義された値が代わりに使用されます。

[`handoff()`][agents.handoffs.handoff] ヘルパーは、渡された特定の `agent` に常に制御を移します。複数の宛先候補がある場合は、宛先ごとに 1 つのハンドオフを登録し、モデルにその中から選ばせてください。独自のハンドオフコードが呼び出し時にどのエージェントを返すかを決定する必要がある場合にのみ、カスタム [`Handoff`][agents.handoffs.Handoff] を使用してください。

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

## ハンドオフ入力

状況によっては、 LLM がハンドオフを呼び出すときに何らかのデータを提供してほしい場合があります。たとえば、「エスカレーションエージェント」へのハンドオフを想像してみてください。ログに記録できるように、モデルに理由を提供してほしい場合があります。

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

`input_type` は、ハンドオフツール呼び出し自体の引数を表します。 SDK はそのスキーマをハンドオフツールの `parameters` としてモデルに公開し、返された JSON をローカルで検証して、解析済みの値を `on_handoff` に渡します。

これは次のエージェントのメイン入力を置き換えるものではなく、別の宛先を選択するものでもありません。 [`handoff()`][agents.handoffs.handoff] ヘルパーは引き続き、ラップした特定のエージェントへ転送し、受け取り側のエージェントは [`input_filter`][agents.handoffs.Handoff.input_filter] またはネストされたハンドオフ履歴設定で変更しない限り、引き続き会話履歴を参照します。

`input_type` は [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] とも別のものです。ローカルにすでにあるアプリケーション状態や依存関係ではなく、ハンドオフ時にモデルが決定するメタデータには `input_type` を使用してください。

### `input_type` の使用タイミング

ハンドオフに `reason` 、 `language` 、 `priority` 、 `summary` など、モデルが生成する小さなメタデータが必要な場合に `input_type` を使用してください。たとえば、トリアージエージェントは `{ "reason": "duplicate_charge", "priority": "high" }` とともに返金エージェントへハンドオフでき、返金エージェントが引き継ぐ前に `on_handoff` でそのメタデータをログに記録したり永続化したりできます。

目的が異なる場合は、別の仕組みを選んでください。

-   既存のアプリケーション状態と依存関係は [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] に置いてください。[コンテキストガイド](context.md)を参照してください。
-   受け取り側のエージェントが参照する履歴を変更したい場合は、 [`input_filter`][agents.handoffs.Handoff.input_filter] 、 [`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] 、または [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を使用してください。
-   複数の専門エージェント候補がある場合は、宛先ごとに 1 つのハンドオフを登録してください。 `input_type` は選択されたハンドオフにメタデータを追加できますが、宛先間の振り分けは行いません。
-   会話を引き渡さずに、ネストされた専門エージェントに構造化入力を渡したい場合は、 [`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool] を優先してください。[ツール](tools.md#structured-input-for-tool-agents)を参照してください。

## 入力フィルター

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、以前の会話履歴全体を参照できるようになります。これを変更したい場合は、 [`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、 [`HandoffInputData`][agents.handoffs.HandoffInputData] を通じて既存の入力を受け取り、新しい `HandoffInputData` を返す必要がある関数です。

[`HandoffInputData`][agents.handoffs.HandoffInputData] には次が含まれます。

-   `input_history`: `Runner.run(...)` が開始する前の入力履歴です。
-   `pre_handoff_items`: ハンドオフが呼び出されたエージェントターンより前に生成されたアイテムです。
-   `new_items`: ハンドオフ呼び出しとハンドオフ出力アイテムを含む、現在のターン中に生成されたアイテムです。
-   `input_items`: セッション履歴用に `new_items` をそのまま保ちながらモデル入力をフィルタリングできるよう、 `new_items` の代わりに次のエージェントへ転送する任意のアイテムです。
-   `run_context`: ハンドオフが呼び出された時点でアクティブな [`RunContextWrapper`][agents.run_context.RunContextWrapper] です。

ネストされたハンドオフはオプトインのベータとして利用でき、安定化が進むまではデフォルトで無効です。 [`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を有効にすると、ランナーは以前の会話記録を 1 つの assistant 要約メッセージにまとめ、それを `<CONVERSATION HISTORY>` ブロックで包みます。このブロックには、同じ実行中に複数のハンドオフが発生した場合に新しいターンが追加され続けます。 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を通じて独自のマッピング関数を提供し、完全な `input_filter` を書くことなく、生成されたメッセージを置き換えることができます。このオプトインは、ハンドオフと実行のどちらも明示的な `input_filter` を指定していない場合にのみ適用されます。そのため、ペイロードをすでにカスタマイズしている既存のコード（このリポジトリ内のコード例を含む）は、変更なしで現在の動作を維持します。単一のハンドオフに対してネスト動作を上書きするには、 [`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡します。これにより [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] が設定されます。生成された要約のラッパーテキストだけを変更したい場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] も呼び出せます）。

ハンドオフとアクティブな [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] の両方がフィルターを定義している場合、その特定のハンドオフではハンドオフごとの [`input_filter`][agents.handoffs.Handoff.input_filter] が優先されます。

!!! note

    ハンドオフは単一の実行内にとどまります。入力ガードレールは引き続きチェーン内の最初のエージェントにのみ適用され、出力ガードレールは最終出力を生成するエージェントにのみ適用されます。ワークフロー内の各カスタム関数ツール呼び出しの周囲でチェックが必要な場合は、ツールガードレールを使用してください。

一般的なパターン（たとえば、履歴からすべてのツール呼び出しを削除するなど）がいくつかあり、 [`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより、 `FAQ agent` が呼び出されたときに、履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

LLM がハンドオフを適切に理解できるように、エージェントにハンドオフに関する情報を含めることを推奨します。推奨されるプレフィックスを [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に用意しています。または、 [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```