---
search:
  exclude: true
---
# ハンドオフ

ハンドオフにより、ある エージェント が別の エージェント にタスクを委譲できます。これは、異なる エージェント がそれぞれ異なる分野を専門としているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクを個別に担当する エージェント を用意できます。

ハンドオフは LLM からはツールとして表現されます。たとえば、`Refund Agent` へのハンドオフがある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべての エージェント には [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、これは `Agent` を直接受け取ることも、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ることもできます。

プレーンな `Agent` インスタンスを渡す場合、その [`handoff_description`][agents.agent.Agent.handoff_description]（設定されているとき）がデフォルトのツール説明に追加されます。完全な `handoff()` オブジェクトを書かずに、そのハンドオフをモデルが選択すべきタイミングを示唆するために活用してください。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先の エージェント の指定に加え、任意の上書き設定や入力フィルターを指定できます。

### 基本的な使い方

シンプルなハンドオフの作成方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接利用することも、`handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、さまざまなカスタマイズが可能です。

- `agent`: ハンドオフ先の エージェント です。
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
- `tool_description_override`: `Handoff.default_tool_description()` によるデフォルトのツール説明を上書きします。
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが実行されると分かったタイミングでデータ取得を開始するなどに役立ちます。この関数は エージェント コンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
- `input_type`: ハンドオフが想定する入力の型（任意）。
- `input_filter`: 次の エージェント が受け取る入力をフィルタリングできます。詳細は後述します。
- `is_enabled`: ハンドオフが有効かどうか。真偽値または真偽値を返す関数を指定でき、実行時にハンドオフを動的に有効・無効化できます。

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

状況によっては、ハンドオフの呼び出し時に LLM からいくつかのデータを提供させたい場合があります。たとえば、「エスカレーション エージェント」へのハンドオフを考えてみましょう。ログ用に理由が提供されると便利です。

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

ハンドオフが発生すると、新しい エージェント が会話を引き継ぎ、これまでの会話履歴全体を参照できるかのように振る舞います。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

デフォルトでは、Runner は直前の記録（transcript）を 1 つのアシスタント要約メッセージに畳み込みます（[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を参照）。この要約は、同じ実行中に複数のハンドオフが発生する場合に新しいターンが追記され続ける `<CONVERSATION HISTORY>` ブロックの中に表示されます。生成されたメッセージ全体を置き換えるマッピング関数を自前で提供したい場合は、完全な `input_filter` を書かずに [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を指定できます。このデフォルトは、ハンドオフ側と実行側のどちらからも明示的な `input_filter` が提供されない場合にのみ適用されるため、既にペイロードをカスタマイズしている既存コード（このリポジトリの code examples を含む）は、変更なしで現在の動作を維持します。単一のハンドオフについて入れ子化の動作を上書きしたい場合は、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡して [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定してください。生成された要約のラッパー文言だけを変更する必要がある場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] も）を呼び出してください。

一般的なパターン（たとえば履歴からすべてのツール呼び出しを除去するなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

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

LLM がハンドオフを正しく理解できるように、エージェント にハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、あるいは [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```