---
search:
  exclude: true
---
# Handoffs

Handoffs は、あるエージェントが別のエージェントにタスクを委譲できるようにするものです。これは、異なるエージェントがそれぞれ別分野を専門とするシナリオで特に有用です。たとえばカスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクを個別に担当するエージェントがいるかもしれません。

Handoffs は LLM に対してはツールとして表現されます。たとえば `Refund Agent` というエージェントへの handoff がある場合、ツール名は `transfer_to_refund_agent` になります。

## Handoff の作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、これは直接 `Agent` を受け取ることも、Handoff をカスタマイズする `Handoff` オブジェクトを受け取ることもできます。

Agents SDK によって提供される [`handoff()`][agents.handoffs.handoff] 関数を使って handoff を作成できます。この関数では、引き渡し先のエージェントに加えて、任意指定のオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

簡単な handoff の作成方法は次のとおりです:

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使用することもできます（`billing_agent` のように）。あるいは `handoff()` 関数を使用できます。

### `handoff()` 関数による handoffs のカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数で詳細をカスタマイズできます。

-   `agent`: 引き渡し先のエージェントです。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` の既定ツール説明を上書きします。
-   `on_handoff`: handoff が呼び出されたときに実行されるコールバック関数です。handoff が呼ばれたと分かったらすぐにデータ取得を開始するような用途に便利です。この関数はエージェントコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: handoff が想定する入力の型（任意）。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は下記を参照してください。
-   `is_enabled`: handoff が有効かどうか。真偽値、または真偽値を返す関数を指定でき、実行時に handoff を動的に有効・無効化できます。

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

## Handoff の入力

状況によっては、LLM が handoff を呼び出す際にデータを提供することを望む場合があります。たとえば「エスカレーションエージェント」への handoff を考えてみてください。理由を提供してもらい、記録したいかもしれません。

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

handoff が発生すると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴全体を閲覧できるかのように動作します。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] として受け取り、新しい `HandoffInputData` を返す関数です。

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

1. これは `FAQ agent` が呼び出されたときに、履歴からツールを自動的にすべて削除します。

## 推奨プロンプト

LLM が handoffs を正しく理解できるようにするため、エージェントに handoffs に関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```