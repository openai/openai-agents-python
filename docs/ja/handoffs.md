---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、 エージェント があるタスクを別の エージェント に委任できます。これは、複数の エージェント がそれぞれ異なる分野を専門とするシナリオで特に有用です。たとえばカスタマーサポートアプリでは、注文状況、返金、 FAQ など、それぞれのタスクを専任で処理する エージェント を用意できます。

ハンドオフは、 LLM からはツールとして表現されます。そのため、`Refund Agent` という エージェント へのハンドオフの場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべての エージェント には [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、直接 `Agent` を渡すか、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すかを選択できます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先の エージェント を指定できるほか、各種オーバーライドや入力フィルターも設定できます。

### 基本的な使用方法

シンプルなハンドオフを作成する例を示します:

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接渡すことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使うと、以下の項目をカスタマイズできます。

- `agent`: ハンドオフ先の エージェント です。  
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。  
- `tool_description_override`: `Handoff.default_tool_description()` の既定ツール説明を上書きします。  
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されたとわかった時点でデータ取得を開始するなどの用途に便利です。この関数は エージェント コンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御されます。  
- `input_type`: ハンドオフで想定される入力の型 (任意)。  
- `input_filter`: 次の エージェント が受け取る入力をフィルタリングします。詳細は後述します。  

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

状況によっては、ハンドオフを呼び出す際に LLM から追加データを渡したい場合があります。たとえば「 Escalation agent 」へのハンドオフでは、記録用に理由を渡したいかもしれません。

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

ハンドオフが行われると、新しい エージェント が会話を引き継ぎ、これまでの会話履歴をすべて閲覧できます。これを変更したい場合は [`input_filter`][agents.handoffs.Handoff.input_filter] を設定します。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

よく使われるパターン (たとえば履歴からすべてのツール呼び出しを削除するなど) は [`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより、`FAQ agent` が呼び出されたときに履歴からすべてのツール呼び出しが自動で削除されます。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるよう、 エージェント のプロンプトにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨プレフィックスを用意しているほか、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出すことで、推奨データを自動でプロンプトに追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```