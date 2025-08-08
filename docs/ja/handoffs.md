---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、あるエージェントがタスクを別のエージェントに委任できます。これは、複数のエージェントがそれぞれ異なる分野を専門としている場合に特に便利です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ など個別のタスクをそれぞれ担当するエージェントを用意できるでしょう。

ハンドオフは、 LLM から見るとツールとして表現されます。そのため、`Refund Agent` というエージェントへのハンドオフがある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには `handoffs` パラメーターがあり、直接 `Agent` を渡すことも、ハンドオフをカスタマイズした `Handoff` オブジェクトを渡すこともできます。

 Agents SDK が提供する `handoff()` 関数を使用して、ハンドオフを作成できます。この関数では、ハンドオフ先のエージェントを指定し、さらにオーバーライドや入力フィルターを任意で設定できます。

### 基本的な使い方

以下はシンプルなハンドオフを作成する方法です：

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接（`billing_agent` のように）指定することも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使用すると、さまざまな項目をカスタマイズできます。

-   `agent`: ハンドオフ先となるエージェントです。  
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。  
-   `tool_description_override`: `Handoff.default_tool_description()` からの既定のツール説明を上書きします。  
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されたタイミングでデータ取得を開始するなどの用途に便利です。この関数はエージェントコンテキストを受け取り、必要に応じて LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。  
-   `input_type`: ハンドオフが受け取る入力の型（任意）です。  
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

## ハンドオフ入力

状況によっては、 LLM がハンドオフを呼び出す際に追加のデータを渡してほしい場合があります。たとえば「Escalation agent」へのハンドオフでは、理由を受け取り、それを記録したいかもしれません。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、それまでの会話履歴をすべて閲覧できる状態になります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] を介して既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

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

1. これにより、`FAQ agent` が呼び出された際に履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

 LLM がハンドオフを正しく理解できるように、エージェントのプロンプトにハンドオフに関する情報を含めることを推奨します。推奨されるプリフィックスとして `agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX` が用意されているほか、`agents.extensions.handoff_prompt.prompt_with_handoff_instructions` を呼び出してプロンプトに推奨データを自動挿入することもできます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```