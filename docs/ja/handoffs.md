---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、エージェントはタスクを別のエージェントに委任できます。これは、各エージェントが異なる分野を専門とするシナリオで特に便利です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などを個別に担当するエージェントがいる場合があります。

ハンドオフは LLM に対してツールとして表現されます。そのため、`Refund Agent` へのハンドオフがある場合、ツール名は `transfer_to_refund_agent` となります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接指定するか、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡せます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先のエージェントのほか、オーバーライドや入力フィルターをオプションで指定できます。

### 基本的な使い方

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

[`handoff()`][agents.handoffs.handoff] 関数では、次のようなカスタマイズが可能です。

- `agent`: ハンドオフ先のエージェントです。
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
- `tool_description_override`: `Handoff.default_tool_description()` から生成される既定のツール説明を上書きします。
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが発生した瞬間にデータ取得を開始するなどに便利です。この関数はエージェントコンテキストを受け取り、さらに LLM が生成した入力も任意で受け取れます。入力データは `input_type` パラメーターで制御します。
- `input_type`: ハンドオフが受け取る入力の型（任意）です。
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

状況によっては、ハンドオフを呼び出す際に LLM からデータを受け取りたい場合があります。たとえば「エスカレーションエージェント」へのハンドオフでは、ログ用に理由を渡したいかもしれません。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴全体を参照できます。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定します。入力フィルターは [`HandoffInputData`][agents.handoffs.HandoffInputData] を受け取り、新しい `HandoffInputData` を返す関数です。

よくあるパターン（たとえば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. `FAQ agent` が呼び出されると、履歴からツール呼び出しが自動的に削除されます。

## 推奨プロンプト

LLM にハンドオフを正しく理解させるため、エージェントのプロンプトにハンドオフ情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、あるいは [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出してプロンプトに自動で必要な情報を追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```