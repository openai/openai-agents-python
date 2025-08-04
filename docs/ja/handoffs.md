---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、エージェントが他のエージェントへタスクを委譲できます。これは、異なるエージェントがそれぞれ異なる分野を専門としているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、 FAQ などのタスクを個別に処理するエージェントが存在する場合があります。

ハンドオフはLLMに対してツールとして表現されます。そのため、`Refund Agent` というエージェントへのハンドオフがある場合、ツール名は `transfer_to_refund_agent` となります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、直接 `Agent` を渡すことも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使用すると、ハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加えて、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使用方法

以下はシンプルなハンドオフの作成例です。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントを直接渡すことも、`handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、次の項目をカスタマイズできます。

- `agent`: ハンドオフ先のエージェント。  
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` となります。これを上書きできます。  
- `tool_description_override`: `Handoff.default_tool_description()` から生成される既定のツール説明を上書きします。  
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数。ハンドオフが発生した時点でデータ取得を開始するなどに便利です。この関数はエージェントコンテキストを受け取り、オプションでLLMが生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。  
- `input_type`: ハンドオフが受け取る想定の入力型 (任意)。  
- `input_filter`: 次のエージェントが受け取る入力をフィルタリングします。詳細は後述します。  

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

状況によっては、LLMにハンドオフの呼び出し時にデータを渡してほしい場合があります。たとえば、"Escalation agent" へのハンドオフを考えてみましょう。理由を提供しておき、ログに残せるようにしたいかもしれません。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴全体を参照できます。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定します。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

代表的なパターン (たとえば履歴からすべてのツール呼び出しを削除する) が [`agents.extensions.handoff_filters`][] に実装されています。

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

LLMがハンドオフを正しく理解できるよう、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨プレフィックスを用意しているほか、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出すことで、プロンプトに推奨データを自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```