---
search:
  exclude: true
---
# ハンドオフ

ハンドオフにより、エージェントはタスクを別のエージェントに委任できます。これは、異なるエージェントがそれぞれ異なる領域に特化しているシナリオで特に有用です。たとえば、カスタマーサポートのアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ専用に扱うエージェントがいる場合があります。

ハンドオフは、LLM に対してツールとして表現されます。そのため、`Refund Agent` という名前のエージェントへのハンドオフがある場合、ツールは `transfer_to_refund_agent` と呼ばれます。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接受け取るか、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取れます。

プレーンな `Agent` インスタンスを渡すと、それらの [`handoff_description`][agents.agent.Agent.handoff_description]（設定されている場合）がデフォルトのツール説明に追加されます。完全な `handoff()` オブジェクトを書かずに、モデルがそのハンドオフを選ぶべきタイミングを示すヒントとして使ってください。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使って、ハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加えて、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

シンプルなハンドオフを作成する方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使う（`billing_agent` のように）ことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使うと、各種項目をカスタマイズできます。

-   `agent`: ハンドオフ先のエージェントです。
-   `tool_name_override`: デフォルトでは `Handoff.default_tool_name()` 関数が使われ、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` のデフォルトのツール説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されることが分かった時点でデータ取得を開始する、といった用途に便利です。この関数はエージェントコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターによって制御されます。
-   `input_type`: ハンドオフが期待する入力の型（任意）です。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は後述します。
-   `is_enabled`: ハンドオフが有効かどうかです。boolean、または boolean を返す関数を指定でき、実行時に動的にハンドオフを有効/無効にできます。

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

状況によっては、LLM がハンドオフを呼び出す際に、何らかのデータを提供してほしい場合があります。たとえば、「Escalation agent」へのハンドオフを想像してください。ログに残せるように、理由を提供してほしいかもしれません。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴全体を参照できるようになります。これを変更したい場合は [`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

ネストされたハンドオフはオプトインのベータとして利用可能で、安定化のためデフォルトでは無効になっています。[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を有効にすると、runner はそれまでのトランスクリプトを 1 つの assistant サマリーメッセージに折りたたみ、`<CONVERSATION HISTORY>` ブロックでラップします。これにより、同一の run 中に複数回のハンドオフが発生した場合に、新しいターンが追記され続けます。完全な `input_filter` を書かずに生成メッセージを置き換えるために、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] で独自のマッピング関数を指定できます。このオプトインは、ハンドオフ側にも run 側にも明示的な `input_filter` が指定されていない場合にのみ適用されるため、すでにペイロードをカスタマイズしている既存コード（このリポジトリの例を含む）は、変更なしで現在の挙動を維持します。単一のハンドオフに対してネスト挙動を上書きするには、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡してください。これにより [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] が設定されます。生成されたサマリーのラッパー文言だけを変更する必要がある場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] も）を呼び出してください。

一般的なパターン（たとえば履歴からすべてのツール呼び出しを削除するなど）がいくつかあり、これらは [`agents.extensions.handoff_filters`][] に実装されています。

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

LLM がハンドオフを適切に理解できるようにするため、エージェントにはハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスが用意されています。または、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動追加することもできます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```