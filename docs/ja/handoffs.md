---
search:
  exclude: true
---
# ハンドオフ

ハンドオフは、あるエージェントが別のエージェントにタスクを委任できるようにする仕組みです。これは、異なるエージェントがそれぞれ別の領域に特化しているシナリオで特に有用です。例えば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクを個別に担当するエージェントを用意できます。

ハンドオフは LLM からはツールとして表現されます。たとえば、`Refund Agent` というエージェントにハンドオフする場合、そのツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接渡すか、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡せます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加えて、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

以下はシンプルなハンドオフの作成方法です。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントを直接使っても、`handoff()` 関数を使ってもかまいません。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、さまざまな点をカスタマイズできます。

- `agent`: ハンドオフ先のエージェントです。
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
- `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明を上書きします。
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼ばれた時点でデータ取得を開始するなどに便利です。この関数はエージェントコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
- `input_type`: ハンドオフで期待する入力の型（任意）です。
- `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は下記を参照してください。
- `is_enabled`: ハンドオフを有効にするかどうか。真偽値、または実行時に動的に有効・無効を切り替える真偽値を返す関数を指定できます。

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

状況によっては、ハンドオフを呼び出す際に LLM にデータの提供を求めたい場合があります。たとえば「エスカレーションエージェント」へのハンドオフを考えてみてください。理由を提供してもらい、ログに残したいことがあるでしょう。

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

ハンドオフが行われると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴全体を参照できるかのように振る舞います。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] として受け取り、新しい `HandoffInputData` を返す関数です。

よくあるパターン（例：履歴からすべてのツール呼び出しを削除する）は、[`agents.extensions.handoff_filters`][] に実装済みです。

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

LLM がハンドオフを正しく理解できるようにするため、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出してプロンプトに推奨データを自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```