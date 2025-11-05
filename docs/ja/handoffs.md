---
search:
  exclude: true
---
# Handoffs

Handoffs は、あるエージェントが別のエージェントへタスクを委譲できるようにするものです。これは、異なるエージェントがそれぞれ異なる分野を専門としているシナリオで特に有用です。たとえば、カスタマーサポートのアプリでは、注文状況、返金、FAQ などをそれぞれ担当するエージェントがいるかもしれません。

Handoffs は LLM からはツールとして表現されます。たとえば `Refund Agent` に対する handoff がある場合、そのツール名は `transfer_to_refund_agent` になります。

## Creating a handoff

すべてのエージェントは [`handoffs`][agents.agent.Agent.handoffs] パラメーターを持ち、これは `Agent` を直接渡すことも、Handoff をカスタマイズする `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使って handoff を作成できます。この関数では、委譲先のエージェントに加え、任意のオーバーライドや入力フィルターを指定できます。

### Basic Usage

以下はシンプルな handoff の作成方法です。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントを直接使うことも、`handoff()` 関数を使うこともできます。

### Customizing handoffs via the `handoff()` function

[`handoff()`][agents.handoffs.handoff] 関数で各種カスタマイズが可能です。

-   `agent`: 委譲先のエージェントです。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` が使われ、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明を上書きします。
-   `on_handoff`: handoff が呼び出されたときに実行されるコールバック関数です。handoff が呼ばれた時点でデータ取得を開始するなどに便利です。この関数はエージェントのコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: handoff が受け取る入力の型（任意）。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳しくは以下を参照してください。
-   `is_enabled`: handoff を有効にするかどうか。真偽値、または真偽値を返す関数を受け付けるため、実行時に動的に有効・無効を切り替えられます。

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

## Handoff inputs

状況によっては、handoff を呼び出す際に LLM に何らかのデータを提供してほしい場合があります。たとえば「エスカレーション エージェント」への handoff を想定してください。記録のために理由を提供してほしい、といったケースです。

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

handoff が発生すると、新しいエージェントが会話を引き継いだかのように、これまでの会話履歴全体を参照できます。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

いくつかの一般的なパターン（たとえば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これは `FAQ agent` が呼ばれたとき、履歴から自動的にすべてのツールを削除します。

## Recommended prompts

LLM に handoffs を正しく理解させるため、各エージェントに handoffs に関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データを自動的にプロンプトへ追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```