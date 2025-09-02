---
search:
  exclude: true
---
# Handoffs

Handoffs は、ある エージェント が別の エージェント にタスクを委譲できるようにします。これは、異なる エージェント がそれぞれ別個の分野を専門としているシナリオで特に有用です。例えば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクを個別に扱う エージェント がいるかもしれません。

Handoffs は ツール として LLM に提示されます。たとえば、`Refund Agent` に handoff する場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべての エージェント には [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接渡すか、Handoff をカスタマイズする `Handoff` オブジェクトを渡すことができます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使って handoff を作成できます。この関数では、handoff 先の エージェント に加えて、任意の上書き設定や入力フィルターを指定できます。

### 基本的な使い方

以下のように、シンプルな handoff を作成できます。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェント を直接使う（`billing_agent` のように）ことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数による handoffs のカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、さまざまなカスタマイズが可能です。

-   `agent`: handoff 先の エージェント です。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` が使われ、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` の既定のツール説明を上書きします。
-   `on_handoff`: handoff が呼び出されたときに実行されるコールバック関数です。handoff が呼ばれたことが分かった時点でデータ取得を開始する、といった用途に便利です。この関数はエージェントのコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: handoff が期待する入力の型（任意）。
-   `input_filter`: 次の エージェント が受け取る入力をフィルタリングできます。詳細は下記を参照してください。
-   `is_enabled`: handoff が有効かどうか。真偽値または真偽値を返す関数を指定でき、実行時に動的に handoff を有効・無効にできます。

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

状況によっては、handoff を呼び出す際に LLM にデータを提供してほしいことがあります。例えば、「Escalation agent」への handoff を考えてみましょう。ログのために理由を提供してもらいたい場合があります。

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

handoff が発生すると、新しい エージェント が会話を引き継ぎ、これまでの会話履歴全体を閲覧できるかのように振る舞います。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] として受け取り、新しい `HandoffInputData` を返す関数です。

よくあるパターン（例えば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより、`FAQ agent` が呼ばれたときに履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

LLM が handoffs を正しく理解できるようにするため、エージェント に handoffs に関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```