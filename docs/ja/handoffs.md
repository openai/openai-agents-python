---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、あるエージェントが別のエージェントにタスクを委譲できます。これは、異なるエージェントがそれぞれ得意分野を持っている場面で特に有用です。たとえば、カスタマーサポートアプリでは、注文状況・返金・FAQ などをそれぞれ担当するエージェントを用意することがあります。

ハンドオフは LLM からはツールとして扱われます。したがって `Refund Agent` というエージェントへのハンドオフがある場合、ツール名は `transfer_to_refund_agent` となります。

## ハンドオフの作成

すべてのエージェントには `handoffs` パラメーターがあり、ここには `Agent` を直接渡すことも、ハンドオフをカスタマイズした `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する `handoff()` 関数を使ってハンドオフを作成できます。この関数では、委譲先のエージェントを指定できるほか、各種オーバーライドや入力フィルターも設定できます。

### 基本的な使い方

シンプルなハンドオフの作成例は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントを直接渡す方法と、`handoff()` 関数を使う方法のどちらも利用できます。

### `handoff()` 関数によるハンドオフのカスタマイズ

`handoff()` 関数では、さまざまなカスタマイズが可能です。

- `agent`: タスクを委譲するエージェント。  
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` に評価されます。ここを上書きできます。  
- `tool_description_override`: `Handoff.default_tool_description()` によるデフォルトのツール説明を上書きします。  
- `on_handoff`: ハンドオフ実行時に呼び出されるコールバック関数。ハンドオフが呼び出された瞬間にデータ取得を開始するなどに便利です。この関数はエージェントコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。この入力データは `input_type` パラメーターで制御します。  
- `input_type`: ハンドオフで期待される入力の型（任意）。  
- `input_filter`: 次のエージェントが受け取る入力をフィルターできます。詳細は後述します。  

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

状況によっては、LLM がハンドオフを呼び出す際に何らかのデータを渡してほしい場合があります。たとえば「Escalation agent（エスカレーション エージェント）」へのハンドオフでは、ログ用に理由を渡してもらいたいかもしれません。

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

ハンドオフが行われると、新しいエージェントが会話を引き継ぎ、以前の会話履歴をすべて閲覧できる状態になります。これを変更したい場合は `input_filter` を設定してください。入力フィルターは `HandoffInputData` として渡された既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

よくあるパターン（履歴からすべてのツール呼び出しを削除するなど）は、`agents.extensions.handoff_filters` に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより `FAQ agent` が呼び出された際に、履歴からすべてのツール呼び出しが自動的に削除されます。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるよう、エージェント内にハンドオフに関する情報を含めることを推奨します。`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX` に推奨のプリフィックスが用意されているほか、`agents.extensions.handoff_prompt.prompt_with_handoff_instructions` を呼び出すことで推奨情報をプロンプトに自動追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```