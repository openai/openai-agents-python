---
search:
  exclude: true
---
# Handoffs

Handoffs により、あるエージェントがタスクを別のエージェントに委譲できます。これは、異なるエージェントがそれぞれ特定分野を専門とするシナリオで特に便利です。たとえばカスタマーサポートアプリでは、注文状況、払い戻し、FAQ などを個別に処理するエージェントがいる場合があります。

Handoffs は LLM からはツールとして表現されます。たとえば `Refund Agent` への handoff がある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] というパラメーターがあり、`Agent` を直接渡すことも、ハンドオフをカスタマイズした `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使って handoff を作成できます。この関数では、委譲先のエージェントの指定に加えて、各種オーバーライドや入力フィルターを設定できます。

### 基本的な使い方

シンプルな handoff を作成する方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントを直接指定することも、`handoff()` 関数を使用することもできます。

### `handoff()` 関数による handoff のカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では以下の項目をカスタマイズできます。

-   `agent`: 委譲先となるエージェントです。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` という形式になります。ここを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明を上書きします。
-   `on_handoff`: handoff が呼び出された際に実行されるコールバック関数です。handoff が行われた時点でデータ取得を開始したい場合などに便利です。この関数はエージェントコンテキストを受け取り、必要であれば LLM が生成した入力も受け取れます。どの入力データが渡されるかは `input_type` パラメーターで制御します。
-   `input_type`: handoff が想定する入力の型です (任意)。
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

## Handoff の入力

状況によっては、LLM が handoff を呼び出す際に何らかのデータを渡してほしい場合があります。たとえば「Escalation agent」への handoff では、ログ用に理由も渡したいといったケースです。

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

## Input filters

Handoff が発生すると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴全体を閲覧できる状態になります。これを変更したい場合は [`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。Input filter は、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] として受け取り、新しい `HandoffInputData` を返す関数です。

よくあるパターン (たとえば履歴からすべてのツール呼び出しを取り除くなど) は [`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより `FAQ agent` が呼び出されたとき、履歴から自動で全ツールが削除されます。

## 推奨プロンプト

LLM が handoffs を正しく理解するように、エージェント内に handoff に関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] という推奨プレフィックスを用意しているほか、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出すことで、プロンプトに自動で必要なデータを追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```