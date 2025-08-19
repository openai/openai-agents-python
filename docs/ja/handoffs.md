---
search:
  exclude: true
---
# ハンドオフ

ハンドオフは、あるエージェントが別のエージェントにタスクを委譲できるようにするものです。これは、異なるエージェントがそれぞれ異なる分野を専門にしているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ専門に扱うエージェントが存在するかもしれません。

ハンドオフは LLM に対してツールとして表現されます。たとえば、`Refund Agent` というエージェントへのハンドオフがある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントは [`handoffs`][agents.agent.Agent.handoffs] パラメーターを持ち、これは `Agent` を直接受け取ることも、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ることもできます。

エージェントへのハンドオフは、Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数で作成できます。この関数では、委譲先のエージェントに加えて、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

以下は、簡単なハンドオフの作成方法です。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使用する（`billing_agent` のように）ことも、`handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、さまざまなカスタマイズが可能です。

-   `agent`: ハンドオフ先のエージェントです。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、これは `transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されることがわかった時点でのデータ取得の開始などに有用です。この関数はエージェントのコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: ハンドオフで想定される入力の型（任意）です。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は以下を参照してください。
-   `is_enabled`: ハンドオフを有効にするかどうかです。真偽値、または真偽値を返す関数を指定でき、実行時にハンドオフを動的に有効化・無効化できます。

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

状況によっては、ハンドオフを呼び出す際に LLM にいくらかのデータを提供させたい場合があります。たとえば、「エスカレーション エージェント」へのハンドオフを考えてみてください。ログのために理由を受け取りたいかもしれません。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継いだかのようになり、前の会話履歴全体を参照できます。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

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

1. これは、`FAQ agent` が呼び出されたときに履歴からすべてのツールを自動的に削除します。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるようにするため、エージェント内にハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、あるいは [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨情報をプロンプトに自動追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```