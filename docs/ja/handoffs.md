---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、あるエージェントがタスクを別のエージェントに委任できます。これは、異なるエージェントがそれぞれ特定分野を専門とするシナリオで特に便利です。たとえばカスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ担当するエージェントが存在する場合があります。

ハンドオフは LLM に対してツールとして表現されます。そのため `Refund Agent` という名前のエージェントへハンドオフする場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] というパラメーターがあり、直接 `Agent` を渡すことも、ハンドオフをカスタマイズした `Handoff` オブジェクトを渡すこともできます。

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

1. エージェントを直接指定する方法（ `billing_agent` など）と、 `handoff()` 関数を使用する方法のどちらも利用できます。

### `handoff()` 関数によるハンドオフのカスタマイズ

 [`handoff()`][agents.handoffs.handoff] 関数を使うと、さまざまなカスタマイズが行えます。

-   `agent`: ハンドオフ先となるエージェントです。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` という名前になります。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` から生成される既定のツール説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが発生した時点でデータ取得を開始するなどの用途に便利です。この関数はエージェントコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: ハンドオフで想定される入力の型（オプション）。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングします。詳細は後述します。

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

状況によっては、ハンドオフを呼び出すときに LLM から追加データを渡したい場合があります。たとえば「 Escalation agent 」へのハンドオフを考えてみましょう。ログに記録できるよう、理由を渡してほしいことがあります。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴をすべて閲覧できる状態になります。これを変更したい場合は [`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、 [`HandoffInputData`][agents.handoffs.HandoffInputData] を介して既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

履歴からすべてのツール呼び出しを削除するといった一般的なパターンは、 [`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. `FAQ agent` が呼び出されたときに、履歴からすべてのツール呼び出しが自動的に削除されます。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるよう、エージェントのプロンプトにハンドオフに関する情報を含めることを推奨します。推奨されるプレフィックスは [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に用意されています。または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出すことで、推奨データを自動的にプロンプトへ追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```