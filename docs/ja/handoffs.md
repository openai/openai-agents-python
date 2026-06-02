---
search:
  exclude: true
---
# ハンドオフ

ハンドオフにより、エージェントはタスクを別のエージェントに委任できます。これは、異なるエージェントが別々の領域に特化しているシナリオで特に有用です。たとえば、カスタマーサポートアプリには、注文状況、返金、FAQ などのタスクをそれぞれ専門的に扱うエージェントがあるかもしれません。

ハンドオフは LLM に対してツールとして表現されます。そのため、`Refund Agent` という名前のエージェントへのハンドオフがある場合、そのツールは `transfer_to_refund_agent` と呼ばれます。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、これは `Agent` を直接受け取ることも、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ることもできます。

単純な `Agent` インスタンスを渡した場合、それらの [`handoff_description`][agents.agent.Agent.handoff_description]（設定されている場合）が既定のツール説明に追加されます。完全な `handoff()` オブジェクトを書かずに、モデルがそのハンドオフを選ぶべきタイミングを示すために使用してください。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使用して、ハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加え、省略可能なオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

簡単なハンドオフは次のように作成できます。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを（`billing_agent` のように）直接使用することも、`handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、さまざまな要素をカスタマイズできます。

-   `agent`: 処理のハンドオフ先となるエージェントです。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` 関数が使用され、これは `transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` の既定のツール説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されることが分かった時点でデータ取得を開始する、といった用途に便利です。この関数はエージェントコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御されます。
-   `input_type`: ハンドオフのツール呼び出し引数のスキーマです。設定されている場合、解析済みのペイロードが `on_handoff` に渡されます。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は下記を参照してください。
-   `is_enabled`: ハンドオフが有効かどうかです。これはブール値、またはブール値を返す関数にできます。これにより、実行時にハンドオフを動的に有効化または無効化できます。
-   `nest_handoff_history`: RunConfig レベルの `nest_handoff_history` 設定に対する、呼び出しごとの任意のオーバーライドです。`None` の場合、アクティブな実行設定で定義された値が代わりに使用されます。

[`handoff()`][agents.handoffs.handoff] ヘルパーは、常に渡された特定の `agent` に制御を移します。複数の宛先候補がある場合は、宛先ごとに 1 つのハンドオフを登録し、モデルにそれらの中から選ばせてください。独自のハンドオフコードが、呼び出し時にどのエージェントを返すかを決定する必要がある場合にのみ、カスタム [`Handoff`][agents.handoffs.Handoff] を使用してください。

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

状況によっては、ハンドオフを呼び出す際に LLM に何らかのデータを提供させたい場合があります。たとえば、「エスカレーションエージェント」へのハンドオフを想像してください。理由を提供させて、それをログに記録したい場合があります。

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

`input_type` は、ハンドオフツール呼び出し自体の引数を記述します。SDK はそのスキーマをハンドオフツールの `parameters` としてモデルに公開し、返された JSON をローカルで検証して、解析済みの値を `on_handoff` に渡します。

これは次のエージェントのメイン入力を置き換えるものではなく、別の宛先を選ぶものでもありません。[`handoff()`][agents.handoffs.handoff] ヘルパーは引き続き、ラップした特定のエージェントに転送し、受信側エージェントは [`input_filter`][agents.handoffs.Handoff.input_filter] またはネストされたハンドオフ履歴設定で変更しない限り、会話履歴を参照します。

`input_type` は [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] とも別です。`input_type` は、ハンドオフ時にモデルが決定するメタデータに使用し、アプリケーション状態やローカルにすでにある依存関係には使用しないでください。

### `input_type` の使用場面

`input_type` は、ハンドオフで `reason`、`language`、`priority`、`summary` など、モデルが生成する小さなメタデータが必要な場合に使用します。たとえば、トリアージエージェントは `{ "reason": "duplicate_charge", "priority": "high" }` を付けて返金エージェントにハンドオフでき、`on_handoff` は返金エージェントが引き継ぐ前にそのメタデータをログに記録したり永続化したりできます。

目的が異なる場合は、別の仕組みを選択してください。

-   既存のアプリケーション状態と依存関係は [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] に入れてください。[コンテキストガイド](context.md) を参照してください。
-   受信側エージェントが参照する履歴を変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter]、[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]、または [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を使用してください。
-   複数の専門エージェント候補がある場合は、宛先ごとに 1 つのハンドオフを登録してください。`input_type` は選択されたハンドオフにメタデータを追加できますが、宛先間の振り分けは行いません。
-   会話を転送せずにネストされた専門エージェントへ構造化入力を渡したい場合は、[`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool] を優先してください。[ツール](tools.md#structured-input-for-tool-agents)を参照してください。

## 入力フィルター

ハンドオフが発生すると、新しいエージェントが会話を引き継いだかのように、以前の会話履歴全体を参照できるようになります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で既存の入力を受け取り、新しい `HandoffInputData` を返す必要がある関数です。

[`HandoffInputData`][agents.handoffs.HandoffInputData] には次が含まれます。

-   `input_history`: `Runner.run(...)` が開始される前の入力履歴です。
-   `pre_handoff_items`: ハンドオフが呼び出されたエージェントターンの前に生成された項目です。
-   `new_items`: 現在のターン中に生成された項目です。ハンドオフ呼び出しとハンドオフ出力項目を含みます。
-   `input_items`: `new_items` の代わりに次のエージェントへ転送する任意の項目です。`new_items` をセッション履歴用にそのまま保持しながら、モデル入力をフィルタリングできます。
-   `run_context`: ハンドオフが呼び出された時点でアクティブな [`RunContextWrapper`][agents.run_context.RunContextWrapper] です。

ネストされたハンドオフはオプトインのベータとして利用可能で、安定化中のため既定では無効です。[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を有効にすると、ランナーはそれまでのトランスクリプトを単一の assistant 要約メッセージにまとめ、それを `<CONVERSATION HISTORY>` ブロックでラップします。このブロックには、同じ実行中に複数のハンドオフが発生した場合、新しいターンが追加され続けます。完全な `input_filter` を書かずに生成されたメッセージを置き換えるには、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] 経由で独自のマッピング関数を提供できます。このオプトインは、ハンドオフと実行のどちらも明示的な `input_filter` を提供していない場合にのみ適用されるため、すでにペイロードをカスタマイズしている既存のコード（このリポジトリのコード例を含む）は、変更なしで現在の動作を維持します。単一のハンドオフについてネスト動作を上書きするには、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡します。これにより [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] が設定されます。生成された要約のラッパーテキストを変更するだけでよい場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（および必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

ハンドオフとアクティブな [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] の両方でフィルターが定義されている場合、その特定のハンドオフでは、ハンドオフごとの [`input_filter`][agents.handoffs.Handoff.input_filter] が優先されます。

!!! note

    ハンドオフは単一の実行内に留まります。入力ガードレールは引き続きチェーン内の最初のエージェントにのみ適用され、出力ガードレールは最終出力を生成するエージェントにのみ適用されます。ワークフロー内の各カスタム関数ツール呼び出しの周辺でチェックが必要な場合は、ツールガードレールを使用してください。

一般的なパターン（たとえば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより、`FAQ agent` が呼び出されたときに、履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

LLM がハンドオフを適切に理解できるように、エージェントにハンドオフに関する情報を含めることをおすすめします。推奨プレフィックスは [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に用意されています。または、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```