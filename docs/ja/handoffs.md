---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを利用すると、エージェントがタスクを別のエージェントに委任できます。これは、複数のエージェントがそれぞれ異なる分野を専門としている場合に特に便利です。たとえば、カスタマーサポート アプリでは、注文状況、返金、FAQ などのタスクを個別に担当するエージェントを用意できます。

ハンドオフは LLM からはツールとして扱われます。たとえば `Refund Agent` というエージェントへのハンドオフなら、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接渡すことも、ハンドオフをカスタマイズした `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先のエージェントを指定し、さらにオーバーライドや入力フィルターをオプションで設定できます。

### 基本的な使い方

シンプルなハンドオフを作成する方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントを直接渡すことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使うと、さまざまなカスタマイズが可能です。

- `agent`: ハンドオフ先となるエージェントです。  
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` という形式になります。これを上書きできます。  
- `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明を上書きします。  
- `on_handoff`: ハンドオフが呼び出された際に実行されるコールバック関数です。ハンドオフが発生した直後にデータ取得を開始したい場合などに便利です。この関数はエージェント コンテキストを受け取り、必要に応じて LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。  
- `input_type`: ハンドオフで受け取る入力の型（オプション）。  
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

状況によっては、ハンドオフ呼び出し時に LLM からいくつかのデータを受け取りたい場合があります。たとえば「 Escalation agent 」へのハンドオフでは、記録用に理由を提供させたいことがあります。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴をすべて閲覧できます。これを変更したい場合は [`input_filter`][agents.handoffs.Handoff.input_filter] を設定してください。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] として受け取り、新しい `HandoffInputData` を返す関数です。

履歴からすべてのツール呼び出しを削除するなど、よく使われるパターンは [`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. `FAQ agent` が呼び出されたときに履歴からすべてのツールを自動的に削除します。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるよう、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスが用意されているほか、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出してプロンプトに自動で推奨データを追加することもできます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```