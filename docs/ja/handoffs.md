---
search:
  exclude: true
---
# Handoffs

Handoffs は、ある エージェント が別の エージェント にタスクを委譲できるようにする仕組みです。これは、異なる エージェント がそれぞれ異なる分野を専門とするシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクを個別に処理する エージェント が存在するかもしれません。

Handoffs は LLM に対してツールとして表現されます。たとえば `Refund Agent` という エージェント へのハンドオフがある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべての エージェント は [`handoffs`][agents.agent.Agent.handoffs] パラメーターを持ち、これは `Agent` を直接渡すか、Handoff をカスタマイズする `Handoff` オブジェクトを渡すことができます。

プレーンな `Agent` インスタンスを渡す場合、その [`handoff_description`][agents.agent.Agent.handoff_description]（設定されているとき）はデフォルトのツール説明に追加されます。完全な `handoff()` オブジェクトを書かずに、そのハンドオフをモデルが選ぶべきタイミングを示唆するために使用します。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、引き渡し先の エージェント に加えて、オプションのオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

次のようにシンプルなハンドオフを作成できます:

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接使うことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、さまざまなカスタマイズが可能です。

- `agent`: 引き渡し先の エージェント です。
- `tool_name_override`: デフォルトでは `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
- `tool_description_override`: `Handoff.default_tool_description()` によるデフォルトのツール説明を上書きします。
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されることが分かった時点でデータ取得を開始する、といった用途に便利です。この関数は エージェント のコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
- `input_type`: ハンドオフが想定する入力の型（任意）です。
- `input_filter`: 次の エージェント が受け取る入力をフィルタリングできます。詳細は以下を参照してください。
- `is_enabled`: ハンドオフが有効かどうか。ブール値、またはブール値を返す関数を指定でき、実行時に動的に有効化/無効化できます。

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

状況によっては、ハンドオフ呼び出し時に LLM にいくらかのデータを提供させたい場合があります。たとえば「エスカレーション エージェント」へのハンドオフを想像してください。ログのために理由を提供したい、というようなケースです。

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

## 入力フィルター

ハンドオフが発生すると、新しい エージェント が会話を引き継ぎ、これまでの会話履歴全体を確認できるようになります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

デフォルトでは、Runner は直前のトランスクリプトを 1 件の assistant サマリーメッセージに折りたたみます（[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を参照）。このサマリーは `<CONVERSATION HISTORY>` ブロック内に表示され、同一の実行中に複数のハンドオフが起きた場合は新しいターンが追記されていきます。[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を指定して、完全な `input_filter` を書かずに生成メッセージを置き換えるマッピング関数を提供することも可能です。なお、このデフォルトは、ハンドオフ側および実行側のどちらも明示的な `input_filter` を提供しない場合にのみ適用されます。そのため、すでにペイロードをカスタマイズしている既存のコード（このリポジトリの code examples を含む）は、変更なしで現在の挙動を維持します。単一のハンドオフに対してネスト動作を上書きしたい場合は、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡して、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定してください。生成サマリーのラッパーテキストだけを変更したい場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] も）を呼び出してください。

いくつかの一般的なパターン（たとえば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これは、`FAQ agent` が呼び出されたときに履歴からすべてのツールを自動的に削除します。

## 推奨プロンプト

LLM が handoffs を正しく理解できるようにするため、エージェント に handoffs に関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```