---
search:
  exclude: true
---
# Handoffs

Handoffs は、あるエージェントが別のエージェントにタスクを委任できるようにする機能です。これは、異なるエージェントがそれぞれ別個の分野を専門としている状況で特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ専門に扱うエージェントがいるかもしれません。

Handoffs は LLM に対してツールとして表現されます。たとえば、`Refund Agent` という名前のエージェントへの handoff がある場合、ツール名は `transfer_to_refund_agent` になります。

## Handoff の作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、これは `Agent` を直接渡すか、Handoff をカスタマイズする `Handoff` オブジェクトを受け取れます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使って handoff を作成できます。この関数では、引き渡し先のエージェントに加えて、任意の上書き設定や入力フィルターを指定できます。

### 基本的な使い方

次のようにシンプルな handoff を作成できます。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使う（`billing_agent` のように）ことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数による Handoff のカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数でさまざまにカスタマイズできます。

-   `agent`: 引き渡し先のエージェントです。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` が使われ、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明を上書きします。
-   `on_handoff`: handoff が呼び出されたときに実行されるコールバック関数です。handoff が呼ばれたことがわかった時点でデータ取得を開始する、などに便利です。この関数はエージェントのコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: handoff で想定される入力の型（任意）です。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は以下を参照してください。
-   `is_enabled`: handoff を有効にするかどうか。真偽値または真偽値を返す関数を指定でき、実行時に handoff を動的に有効化・無効化できます。

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

状況によっては、handoff を呼ぶ際に LLM によるデータ提供が必要になることがあります。たとえば「エスカレーションエージェント」への handoff を想定すると、ログ用に理由を渡したくなるかもしれません。

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

handoff が発生すると、新しいエージェントが会話を引き継ぎ、それまでの会話履歴全体を確認できる状態になります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

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

1. これにより、`FAQ agent` が呼ばれたとき、履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

LLM が handoffs を正しく理解できるように、エージェント内に handoffs に関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```