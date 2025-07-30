---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、あるエージェントが別のエージェントにタスクを委任できます。これは、エージェントごとに異なる分野を専門とさせたいシナリオで特に役立ちます。たとえば、カスタマーサポートアプリでは、注文状況、返金、 FAQ などのタスクをそれぞれ担当するエージェントを用意できます。

ハンドオフは LLM に対してはツールとして表現されます。そのため、`Refund Agent` という名前のエージェントへのハンドオフであれば、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、直接 `Agent` を渡すことも、ハンドオフをカスタマイズした `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先のエージェントを指定し、さらに任意でオーバーライドや入力フィルターを設定できます。

### 基本的な使い方

シンプルなハンドオフの例は次のとおりです:

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントをそのまま渡すことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使うと、さまざまなカスタマイズが可能です。

- `agent`: ハンドオフ先のエージェントを指定します。  
- `tool_name_override`: 既定では `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` という形式になります。ここで上書きできます。  
- `tool_description_override`: `Handoff.default_tool_description()` で設定されるデフォルトのツール説明を上書きします。  
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが実行されたと同時にデータ取得を開始するなどの用途に便利です。この関数はエージェントコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御されます。  
- `input_type`: ハンドオフが受け取る入力の型 (任意)。  
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

場合によっては、ハンドオフを呼び出す際に LLM に何らかのデータを渡してほしいことがあります。たとえば「Escalation agent」へのハンドオフでは、ログ用に理由を記録したいかもしれません。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴全体を閲覧できるようになります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定してください。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] を介して既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

よく使われるパターン (たとえば履歴からすべてのツール呼び出しを削除するなど) は、[`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これは `FAQ agent` が呼び出されたときに、履歴からすべてのツールを自動的に削除します。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるように、エージェントにハンドオフに関する情報を組み込むことをお勧めします。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨されるプレフィックスが用意されているほか、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出すことで、プロンプトに推奨事項を自動で追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```