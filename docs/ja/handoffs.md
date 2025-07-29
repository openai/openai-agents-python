---
search:
  exclude: true
---
# ハンドオフ

Handoffs は、エージェント がタスクを別のエージェント に委譲できるしくみです。これは、異なるエージェント がそれぞれ異なる分野を専門としているシナリオで特に有用です。たとえばカスタマーサポートアプリでは、注文状況、返金、FAQ などを個別に処理するエージェント を用意できます。

ハンドオフは LLM からは tool として扱われます。たとえば `Refund Agent` というエージェント へのハンドオフがある場合、その tool 名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェント には [`handoffs`][agents.agent.Agent.handoffs] パラメーター があり、`Agent` を直接指定することも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先のエージェント に加え、オーバーライドや入力フィルターをオプションで指定できます。

### 基本的な使い方

以下はシンプルなハンドオフの作成例です。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェント を直接使うことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、次の項目をカスタマイズできます。

-   `agent`: ハンドオフ先となるエージェント です。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` になります。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` の既定の tool 説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが確定したタイミングでデータ取得を開始するなどに便利です。この関数はエージェント コンテキストを受け取り、`input_type` に応じて LLM が生成した入力も受け取れます。
-   `input_type`: ハンドオフが期待する入力の型 (オプション)。
-   `input_filter`: 次のエージェント が受け取る入力をフィルタリングします。詳しくは後述します。

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

状況によっては、LLM がハンドオフを呼び出す際にデータを渡してほしい場合があります。たとえば「エスカレーション エージェント」へのハンドオフでは、記録用に理由を受け取りたいかもしれません。

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

ハンドオフが発生すると、新しいエージェント が会話を引き継ぎ、これまでの会話履歴全体を閲覧できます。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定します。入力フィルターは [`HandoffInputData`][agents.handoffs.HandoffInputData] を受け取り、新しい `HandoffInputData` を返す関数です。

よくあるパターン (たとえば履歴からすべての tool コールを削除する) は [`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより、`FAQ agent` が呼び出されたとき履歴からすべての tool が自動的に削除されます。

## 推奨プロンプト

LLM にハンドオフを正しく理解させるため、エージェント にハンドオフ情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスが用意されているほか、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出すと、推奨情報をプロンプトに自動追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```