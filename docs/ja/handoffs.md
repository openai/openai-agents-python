# ハンドオフ

ハンドオフを使用すると、あるエージェントが別のエージェントにタスクを委任できます。これは、異なるエージェントがそれぞれ特定の分野を専門としている場合に特に役立ちます。例えば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ専門に処理するエージェントを用意できます。

ハンドオフは、LLM に対してツールとして表現されます。例えば、`Refund Agent` という名前のエージェントへのハンドオフがある場合、そのツールは `transfer_to_refund_agent` と呼ばれます。

## ハンドオフの作成

すべてのエージェントは [`handoffs`][agents.agent.Agent.handoffs] パラメータを持ち、これは直接 `Agent` を指定するか、またはカスタマイズされた `Handoff` オブジェクトを指定できます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使用してハンドオフを作成できます。この関数を使用すると、ハンドオフ先のエージェントを指定し、オプションでオーバーライドや入力フィルターを設定できます。

### 基本的な使用方法

シンプルなハンドオフの作成方法は以下の通りです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接指定する方法（`billing_agent` のように）と、`handoff()` 関数を使用する方法があります。

### `handoff()` 関数を使用したハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使用すると、以下の項目をカスタマイズできます。

- `agent`：ハンドオフ先のエージェントを指定します。
- `tool_name_override`：デフォルトでは `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` という形式になりますが、これをオーバーライドできます。
- `tool_description_override`：デフォルトのツール説明（`Handoff.default_tool_description()`）をオーバーライドします。
- `on_handoff`：ハンドオフが呼び出された際に実行されるコールバック関数です。これは、ハンドオフが呼ばれた時点でデータ取得などの処理を開始する場合に便利です。この関数はエージェントのコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメータで制御されます。
- `input_type`：ハンドオフが期待する入力の型を指定します（オプション）。
- `input_filter`：次のエージェントが受け取る入力をフィルタリングできます。詳細は後述します。

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

特定の状況では、LLM がハンドオフを呼び出す際に何らかのデータを提供するようにしたい場合があります。例えば、「Escalation agent（エスカレーションエージェント）」へのハンドオフを考えると、ログ記録のために理由を提供してほしい場合があります。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、それまでの会話履歴全体を参照できるようになります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] として受け取り、新しい `HandoffInputData` を返す関数です。

よくあるパターン（例えば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] にあらかじめ実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより、`FAQ agent` が呼び出された際に履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

LLM がハンドオフを適切に理解できるようにするため、エージェントのプロンプトにハンドオフに関する情報を含めることを推奨します。推奨されるプレフィックスは [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に用意されています。または、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```