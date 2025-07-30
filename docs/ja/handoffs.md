---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、エージェントはタスクを別のエージェントに委譲できます。これは、異なるエージェントがそれぞれ異なる分野を専門としているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクを個別に処理するエージェントがいる場合があります。

ハンドオフは LLM からはツールとして認識されます。たとえば、`Refund Agent` というエージェントにハンドオフする場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントは [`handoffs`][agents.agent.Agent.handoffs] パラメーターを持っており、ここには直接 `Agent` を渡すことも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加え、オーバーライドや入力フィルターをオプションで指定できます。

### 基本的な使い方

以下のように、シンプルなハンドオフを作成できます。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントを直接指定することも、`handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、次のようなカスタマイズが可能です。

- `agent`: ハンドオフ先のエージェントです。  
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` になります。これを上書きできます。  
- `tool_description_override`: `Handoff.default_tool_description()` の既定の説明を上書きします。  
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが発生したタイミングでデータ取得を開始するなどの用途に便利です。この関数はエージェントコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。どの入力を受け取るかは `input_type` パラメーターで制御します。  
- `input_type`: ハンドオフで想定される入力の型（任意）。  
- `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は後述します。  

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

状況によっては、ハンドオフを呼び出す際に LLM に何らかのデータを渡してほしい場合があります。たとえば、「エスカレーションエージェント」へのハンドオフでは、記録用に理由を受け取りたいことがあります。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、それまでの会話履歴をすべて閲覧できます。これを変更したい場合は [`input_filter`][agents.handoffs.Handoff.input_filter] を設定します。入力フィルターは [`HandoffInputData`][agents.handoffs.HandoffInputData] を受け取り、新しい `HandoffInputData` を返す関数です。

履歴からすべてのツール呼び出しを削除するなど、一般的なパターンはいくつかあり、[`agents.extensions.handoff_filters`][] に実装されています。

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

LLM がハンドオフを正しく理解できるように、エージェントのプロンプトにハンドオフに関する情報を含めることを推奨します。推奨されるプレフィックスは [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に用意されており、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出すことで、自動で推奨データをプロンプトに追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```