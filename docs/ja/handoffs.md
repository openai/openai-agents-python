---
search:
  exclude: true
---
# Handoffs

Handoffs により、ある エージェント が別の エージェント にタスクを委譲できます。これは、異なる エージェント がそれぞれ異なる分野を専門とするシナリオで特に有用です。例えば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ担当する エージェント がいるかもしれません。

Handoffs は LLM に対してツールとして表現されます。例えば `Refund Agent` という エージェント にハンドオフする場合、そのツール名は `transfer_to_refund_agent` になります。

## Handoff の作成

すべての エージェント には [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、これは直接 `Agent` を受け取るか、Handoff をカスタマイズする `Handoff` オブジェクトを受け取ります。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使って handoff を作成できます。この関数では、ハンドオフ先の エージェント を指定し、任意でオーバーライドや入力フィルターを設定できます。

### 基本的な使い方

簡単な handoff の作成方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接渡すことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数による handoff のカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、さまざまなカスタマイズが可能です。

-   `agent`: ハンドオフ先の エージェント です。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` の既定のツール説明を上書きします。
-   `on_handoff`: handoff が呼び出されたときに実行されるコールバック関数です。これは、handoff が実行されることが分かった時点でデータ取得を開始するなどに便利です。この関数は エージェント のコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: handoff が期待する入力の型（任意）。
-   `input_filter`: 次の エージェント が受け取る入力をフィルタリングできます。詳細は以下を参照してください。

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

状況によっては、handoff を呼び出す際に LLM にデータの提供を求めたい場合があります。例えば「エスカレーション エージェント」への handoff を想定してください。記録のために理由を提供してほしい、というようなケースです。

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

handoff が発生すると、新しい エージェント が会話を引き継ぎ、これまでの会話履歴全体を参照できるかのように振る舞います。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] を介して既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

一般的なパターン（例えば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これは、`FAQ agent` が呼び出されたときに履歴から自動的にすべてのツールを削除します。

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