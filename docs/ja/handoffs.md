# ハンドオフ

ハンドオフは、エージェントがタスクを他のエージェントに委任することを可能にします。これは、異なるエージェントが異なる分野に特化しているシナリオで特に有用です。例えば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ専門に扱うエージェントがいるかもしれません。

ハンドオフは LLM にとってツールとして表現されます。したがって、`Refund Agent` という名前のエージェントへのハンドオフがある場合、ツールは `transfer_to_refund_agent` と呼ばれます。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、これは `Agent` を直接取るか、ハンドオフをカスタマイズする `Handoff` オブジェクトを取ることができます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使用してハンドオフを作成できます。この関数を使用すると、ハンドオフ先のエージェントを指定し、オプションでオーバーライドや入力フィルターを設定できます。

### 基本的な使い方

簡単なハンドオフを作成する方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使用することも（`billing_agent` のように）、`handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使用すると、さまざまなカスタマイズが可能です。

-   `agent`: ハンドオフ先のエージェントです。
-   `tool_name_override`: デフォルトでは `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` に解決されます。これをオーバーライドできます。
-   `tool_description_override`: `Handoff.default_tool_description()` からのデフォルトのツール説明をオーバーライドします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されるとすぐにデータ取得を開始するなどに役立ちます。この関数はエージェントコンテキストを受け取り、オプションで LLM が生成した入力も受け取ることができます。入力データは `input_type` パラメーターで制御されます。
-   `input_type`: ハンドオフが期待する入力のタイプ（オプション）。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は以下を参照してください。

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

特定の状況では、LLM がハンドオフを呼び出す際にデータを提供することを望む場合があります。例えば、「エスカレーションエージェント」へのハンドオフを想像してください。理由を提供してログに記録したいかもしれません。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、以前の会話履歴全体を見ることができます。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す必要がある関数です。

一般的なパターン（例えば、履歴からすべてのツール呼び出しを削除する）が [`agents.extensions.handoff_filters`][] に実装されています。

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

LLM がハンドオフを正しく理解するために、エージェントにハンドオフに関する情報を含めることをお勧めします。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨されるプレフィックスがあります。または、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、プロンプトに推奨データを自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```