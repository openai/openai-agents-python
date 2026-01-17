---
search:
  exclude: true
---
# ハンドオフ

ハンドオフは、ある エージェント が別の エージェント にタスクを委譲できるようにする機能です。これは、異なる エージェント がそれぞれ異なる領域を専門にしているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクを個別に担当する エージェント を用意できます。

ハンドオフは LLM に対してツールとして表現されます。たとえば `Refund Agent` という エージェント へのハンドオフがある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべての エージェント には [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、これは直接 `Agent` を受け取ることも、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ることもできます。

プレーンな `Agent` インスタンスを渡す場合、（設定されていれば）その [`handoff_description`][agents.agent.Agent.handoff_description] がデフォルトのツール説明に追加されます。完全な `handoff()` オブジェクトを書かずに、そのハンドオフをモデルが選ぶべきタイミングを示す助けとして使えます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、引き渡し先の エージェント に加えて、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

シンプルなハンドオフの作成方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接使うことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、以下をカスタマイズできます。

- `agent`: 引き渡し先の エージェント です。
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使われ、`transfer_to_<agent_name>` になります。これを上書きできます。
- `tool_description_override`: `Handoff.default_tool_description()` によるデフォルトのツール説明を上書きします。
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが起こることが分かった時点でデータ取得を開始するなどに便利です。この関数は エージェント のコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
- `input_type`: ハンドオフが想定する入力の型（任意）。
- `input_filter`: 次の エージェント が受け取る入力をフィルタリングできます。詳細は下記参照。
- `is_enabled`: ハンドオフが有効かどうか。真偽値または真偽値を返す関数を指定でき、実行時に動的に有効／無効を切り替えられます。

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

## ハンドオフの入力

状況によっては、ハンドオフを呼び出す際に LLM からいくつかのデータを提供してほしい場合があります。例えば「エスカレーション エージェント」へのハンドオフを想定すると、ログのために理由を渡したいことがあります。

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

ハンドオフが起きると、新しい エージェント が会話を引き継ぎ、これまでの会話履歴全体を参照できるようになります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

既定では、Runner は直前の会話記録を 1 つのアシスタント要約メッセージに折りたたみます（[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を参照）。この要約は、同一の実行中に複数のハンドオフが起きた場合に新しいターンを追加し続ける `<CONVERSATION HISTORY>` ブロック内に表示されます。完全な `input_filter` を書かずに生成メッセージを置き換えたい場合は、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] で独自のマッピング関数を指定できます。なお、このデフォルトは、ハンドオフ側と実行側のいずれも明示的な `input_filter` を提供しない場合にのみ適用されます。そのため、既にペイロードをカスタマイズしている既存コード（このリポジトリ内の code examples を含む）は、変更なしで現在の挙動を維持します。単一のハンドオフについて入れ子の有無を上書きしたい場合は、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡して、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定してください。生成要約のラッパーテキストだけを変更したい場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

一般的なパターン（たとえば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これは、`FAQ agent` が呼び出された際に履歴からすべてのツールを自動的に削除します。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるようにするため、エージェント にハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、あるいは [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨情報を自動的にプロンプトへ追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```