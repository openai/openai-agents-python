---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使うと、あるエージェントが別のエージェントにタスクを委譲できます。これは、異なるエージェントがそれぞれ異なる領域を専門にしているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、 FAQ などのタスクをそれぞれ専任で処理するエージェントを用意できます。

ハンドオフは LLM に対してツールとして表現されます。したがって、`Refund Agent` という名前のエージェントへのハンドオフがある場合、そのツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接渡すことも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

プレーンな `Agent` インスタンスを渡す場合、[`handoff_description`][agents.agent.Agent.handoff_description]（設定されている場合）がデフォルトのツール説明に追記されます。これを使うと、完全な `handoff()` オブジェクトを書かなくても、どのときにそのハンドオフをモデルが選ぶべきかを示せます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加えて、任意のオーバーライドや input filter を指定できます。

### 基本的な使い方

シンプルなハンドオフは次のように作成できます。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接（`billing_agent` のように）使うことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使うと、さまざまなカスタマイズができます。

-   `agent`: ハンドオフ先のエージェントです。
-   `tool_name_override`: デフォルトでは `Handoff.default_tool_name()` 関数が使われ、`transfer_to_<agent_name>` に解決されます。これをオーバーライドできます。
-   `tool_description_override`: `Handoff.default_tool_description()` のデフォルトツール説明をオーバーライドします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフ呼び出しが分かった時点でデータ取得を開始する、といった用途に有用です。この関数はエージェントコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御されます。
-   `input_type`: ハンドオフのツール呼び出し引数のスキーマです。設定すると、パース済みペイロードが `on_handoff` に渡されます。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は下記を参照してください。
-   `is_enabled`: ハンドオフを有効にするかどうかです。boolean または boolean を返す関数を指定でき、実行時に動的に有効 / 無効を切り替えられます。
-   `nest_handoff_history`: RunConfig レベルの `nest_handoff_history` 設定を呼び出し単位で上書きする任意設定です。`None` の場合、アクティブな実行設定で定義された値が代わりに使われます。

[`handoff()`][agents.handoffs.handoff] ヘルパーは、常に渡された特定の `agent` に制御を移します。遷移先候補が複数ある場合は、遷移先ごとにハンドオフを 1 つずつ登録し、モデルにその中から選ばせてください。独自のハンドオフコードが呼び出し時に返すエージェントを決定する必要がある場合にのみ、カスタム [`Handoff`][agents.handoffs.Handoff] を使用してください。

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

状況によっては、ハンドオフを呼び出すときに LLM にデータを渡してほしいことがあります。たとえば「Escalation agent」へのハンドオフを考えてみてください。ログに記録できるよう、理由を渡してほしい場合があります。

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

`input_type` は、ハンドオフツール呼び出し自体の引数を記述します。SDK はそのスキーマをハンドオフツールの `parameters` としてモデルに公開し、返された JSON をローカルで検証して、パース済みの値を `on_handoff` に渡します。

これは次のエージェントのメイン入力を置き換えるものではなく、遷移先を変更するものでもありません。[`handoff()`][agents.handoffs.handoff] ヘルパーは、引き続きラップした特定のエージェントへハンドオフします。また、受信側エージェントは、[`input_filter`][agents.handoffs.Handoff.input_filter] やネストされたハンドオフ履歴設定で変更しない限り、会話履歴を引き続き参照します。

`input_type` は [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] とも別物です。`input_type` は、ハンドオフ時にモデルが決定するメタデータに使い、ローカルですでに持っているアプリケーション状態や依存関係には使わないでください。

### `input_type` を使うタイミング

ハンドオフに `reason`、`language`、`priority`、`summary` のような、モデル生成の小さなメタデータが必要な場合に `input_type` を使ってください。たとえば、トリアージエージェントは `{ "reason": "duplicate_charge", "priority": "high" }` を付けて返金エージェントへハンドオフでき、`on_handoff` は返金エージェントに制御が移る前にそのメタデータをログ化または永続化できます。

目的が異なる場合は、別の仕組みを選んでください。

-   既存のアプリケーション状態と依存関係は [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] に入れてください。[context ガイド](context.md)を参照してください。
-   受信側エージェントが見る履歴を変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter]、[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]、または [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を使ってください。
-   複数の専門エージェントが候補にある場合は、遷移先ごとにハンドオフを 1 つずつ登録してください。`input_type` は選ばれたハンドオフにメタデータを追加できますが、遷移先の振り分けはしません。
-   会話を転送せずにネストされた専門エージェント向けの構造化入力が欲しい場合は、[`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool] を優先してください。[tools](tools.md#structured-input-for-tool-agents)を参照してください。

## input filter

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、以前の会話履歴全体を参照できる状態になります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。input filter は、既存入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

[`HandoffInputData`][agents.handoffs.HandoffInputData] には次が含まれます。

-   `input_history`: `Runner.run(...)` 開始前の入力履歴。
-   `pre_handoff_items`: ハンドオフが呼び出されたエージェントターンより前に生成されたアイテム。
-   `new_items`: 現在のターン中に生成されたアイテム（ハンドオフ呼び出しとハンドオフ出力アイテムを含む）。
-   `input_items`: `new_items` の代わりに次のエージェントへ渡す任意のアイテム。これにより、セッション履歴用に `new_items` を保ったまま、モデル入力をフィルタリングできます。
-   `run_context`: ハンドオフ呼び出し時点でアクティブな [`RunContextWrapper`][agents.run_context.RunContextWrapper]。

ネストされたハンドオフは opt-in のベータとして提供されており、安定化のためデフォルトでは無効です。[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を有効にすると、runner はそれまでの transcript を 1 つの assistant 要約メッセージに折りたたみ、同一 run 中に複数のハンドオフが起きると新しいターンが追記され続ける `<CONVERSATION HISTORY>` ブロックに包みます。完全な `input_filter` を書かずに生成メッセージを置き換えたい場合は、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] で独自のマッピング関数を渡せます。この opt-in は、ハンドオフ側と run 側のいずれも明示的な `input_filter` を指定していない場合にのみ適用されるため、すでにペイロードをカスタマイズしている既存コード（このリポジトリのコード例を含む）は変更なしで現在の挙動を維持します。[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡すことで、単一ハンドオフのネスト挙動を上書きできます（これは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定します）。生成要約のラッパーテキストだけを変更したい場合は、エージェント実行前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

ハンドオフ側とアクティブな [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] の両方でフィルターが定義されている場合、その特定ハンドオフではハンドオフ単位の [`input_filter`][agents.handoffs.Handoff.input_filter] が優先されます。

!!! note

    ハンドオフは単一の run 内に留まります。入力ガードレールは依然としてチェーン内の最初のエージェントにのみ適用され、出力ガードレールは最終出力を生成するエージェントにのみ適用されます。ワークフロー内の各カスタム function-tool 呼び出しごとにチェックが必要な場合は、ツールガードレールを使用してください。

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

1. これにより、`FAQ agent` が呼び出されたときに履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

LLM がハンドオフを適切に理解できるように、エージェントにハンドオフ情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨プレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```