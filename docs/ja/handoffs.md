---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、ある エージェント がタスクを別の エージェント に委任できます。これは、異なる エージェント がそれぞれ特定の分野を専門とするシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、 FAQ など各タスクを担当する エージェント を用意することがあります。

ハンドオフは LLM から見るとツールとして表現されます。そのため、`Refund Agent` へのハンドオフがある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべての エージェント には [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、 `Agent` を直接渡すか、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すことができます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先の エージェント を指定し、さらにオーバーライドや入力フィルターを任意で設定できます。

### 基本的な使い方

シンプルなハンドオフを作成する方法は次のとおりです:

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接渡す方法と、 `handoff()` 関数を使用する方法があります。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使うと、さまざまなカスタマイズが可能です。

-   `agent` : ハンドオフ先となる エージェント です。  
-   `tool_name_override` : 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` となります。これを上書きできます。  
-   `tool_description_override` : `Handoff.default_tool_description()` で生成されるデフォルトのツール説明を上書きします。  
-   `on_handoff` : ハンドオフが呼び出された際に実行されるコールバック関数です。ハンドオフが行われた瞬間にデータ取得を開始するなどの用途に便利です。この関数は エージェント のコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データの有無は `input_type` パラメーターで制御します。  
-   `input_type` : ハンドオフが受け取る入力の型（任意）。  
-   `input_filter` : 次の エージェント が受け取る入力をフィルタリングできます。詳細は後述します。  

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

状況によっては、ハンドオフを呼び出す際に LLM からデータを受け取りたいことがあります。たとえば「Escalation agent」へのハンドオフでは、ログ用に理由を渡してもらいたいかもしれません。

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

ハンドオフが発生すると、新しい エージェント が会話を引き継ぎ、これまでの会話履歴全体を閲覧できる状態になります。これを変更したい場合は [`input_filter`][agents.handoffs.Handoff.input_filter] を設定してください。入力フィルターは [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

履歴からすべてのツール呼び出しを削除するなど、よくあるパターンは [`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. `FAQ agent` が呼び出されると、履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるよう、 エージェント のプロンプトにハンドオフに関する情報を含めることを推奨します。推奨されるプレフィックスは [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] にあります。また、 [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出してプロンプトに推奨情報を自動追加することもできます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```