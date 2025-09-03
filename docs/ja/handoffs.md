---
search:
  exclude: true
---
# ハンドオフ

ハンドオフにより、あるエージェントが別のエージェントにタスクを委譲できます。これは、異なるエージェントがそれぞれ別の分野を専門としているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ専任で扱うエージェントがいるかもしれません。

ハンドオフは LLM にはツールとして表現されます。たとえば `Refund Agent` というエージェントにハンドオフする場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントは [`handoffs`][agents.agent.Agent.handoffs] パラメーターを持ち、これは `Agent` を直接渡すことも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加え、任意の上書き設定や入力フィルターを指定できます。

### 基本的な使い方

シンプルなハンドオフの作り方は次のとおりです:

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントを直接使うことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数ではさまざまなカスタマイズが可能です。

-   `agent`: ハンドオフ先のエージェントです。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` 関数が使われ、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` の既定のツール説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフの発動がわかった時点でデータ取得を開始する、といった用途に便利です。この関数はエージェントコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: ハンドオフが想定する入力の型（任意）です。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は下記を参照してください。
-   `is_enabled`: ハンドオフを有効にするかどうか。真偽値、または真偽値を返す関数を指定でき、実行時に動的に有効化/無効化できます。

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

状況によっては、ハンドオフ呼び出し時に LLM にデータを提供してほしいことがあります。たとえば「Escalation agent」へのハンドオフを考えてみましょう。ログのために理由を提供してほしい場合があります。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継いだかのようになり、これまでの会話履歴全体を見ることができます。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

いくつかの一般的なパターン（たとえば履歴からすべてのツールコールを取り除くなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

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

LLM にハンドオフを正しく理解させるため、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```