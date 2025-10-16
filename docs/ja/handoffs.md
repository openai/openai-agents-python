---
search:
  exclude: true
---
# Handoffs

handoffs によって、あるエージェントが別のエージェントへタスクを委譲できます。これは、異なるエージェントが別々の分野を専門としているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ担当するエージェントがいるかもしれません。

handoffs は LLM に対してツールとして表現されます。たとえば、`Refund Agent` という名前のエージェントへの handoff がある場合、そのツール名は `transfer_to_refund_agent` となります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、これは `Agent` を直接渡すか、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ります。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数でハンドオフを作成できます。この関数では、引き渡し先のエージェントに加えて、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

次のようにシンプルなハンドオフを作成できます。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使う（`billing_agent` のように）ことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数による handoffs のカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、さまざまなカスタマイズが可能です。

- `agent`: 引き渡し先のエージェントです。
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
- `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明を上書きします。
- `on_handoff`: handoff が呼び出されたときに実行されるコールバック関数です。handoff が呼び出されることが分かった時点でデータ取得を開始する、といった用途に便利です。この関数はエージェントのコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
- `input_type`: handoff が想定する入力の型（任意）です。
- `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は後述します。
- `is_enabled`: handoff を有効にするかどうかです。真偽値、または真偽値を返す関数を指定でき、実行時に handoff を動的に有効/無効化できます。

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

状況によっては、LLM に handoff を呼び出す際にいくつかのデータを提供してほしい場合があります。たとえば、「エスカレーション エージェント」への handoff を想像してください。記録のために理由を提供してほしいといったケースです。

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

handoff が発生すると、新しいエージェントが会話を引き継ぎ、過去の会話履歴全体を参照できる状態になります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

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

1. これにより、`FAQ agent` が呼び出されたときに履歴から自動的にすべてのツールが削除されます。

## 推奨プロンプト

LLM が handoffs を正しく理解できるように、エージェント内に handoffs に関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データを自動的にプロンプトへ追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```