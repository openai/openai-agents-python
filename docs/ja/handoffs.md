---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、エージェントはタスクを別のエージェントに委任できます。これは、異なるエージェントがそれぞれ特定の分野に特化しているシナリオで特に有用です。たとえば、カスタマーサポート アプリでは、注文状況、返金、FAQ などのタスクをそれぞれ担当するエージェントが存在する場合があります。

ハンドオフは LLM には tool として表現されます。そのため、`Refund Agent` というエージェントへのハンドオフであれば、tool 名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接渡すことも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加え、各種オーバーライドや入力フィルターを指定できます。

### 基本的な使い方

シンプルなハンドオフを作成する方法は次のとおりです:

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントを直接指定することも、`handoff()` 関数を利用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使用すると、さまざまなカスタマイズが可能です。

-   `agent`: ハンドオフ先となるエージェントです。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` により `transfer_to_<agent_name>` が使用されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` で生成される既定の tool 説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが実行された瞬間にデータ取得を開始するなどの用途に便利です。この関数はエージェントのコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: ハンドオフで想定される入力の型です (省略可)。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は後述します。

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

状況によっては、LLM がハンドオフを呼び出す際にデータを渡してほしい場合があります。たとえば「エスカレーション エージェント」へのハンドオフでは、理由を受け取り、それを記録したいことが考えられます。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、それまでの全履歴を閲覧できる状態になります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは [`HandoffInputData`][agents.handoffs.HandoffInputData] を受け取り、新しい `HandoffInputData` を返す関数です。

一般的なパターン (たとえば履歴からすべての tool 呼び出しを削除するなど) は、[`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより `FAQ agent` が呼び出される際、履歴からすべての tool が自動的に除去されます。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるよう、エージェント内にハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨されるプレフィックスを用意しているほか、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出すことで、推奨事項を自動でプロンプトに追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```