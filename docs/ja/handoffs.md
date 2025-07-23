---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、あるエージェントが別のエージェントへタスクを委任できます。これは、異なるエージェントがそれぞれ異なる領域を専門としているシナリオで特に役立ちます。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などを個別に担当するエージェントがいる場合があります。

ハンドオフは LLM に対してツールとして表現されます。たとえば、`Refund Agent` へのハンドオフがある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントは [`handoffs`][agents.agent.Agent.handoffs] パラメーターを持ち、ここには `Agent` を直接渡すことも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使用してハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加え、オプションで上書き設定や入力フィルターを指定できます。

### 基本的な使い方

以下はシンプルなハンドオフの作成例です。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントを直接指定することも、`handoff()` 関数を利用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、次の項目をカスタマイズできます。

- `agent`: ハンドオフ先のエージェント。
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` になります。これを上書きできます。
- `tool_description_override`: `Handoff.default_tool_description()` で生成される既定の説明を上書きします。
- `on_handoff`: ハンドオフが呼び出された際に実行されるコールバック関数。ハンドオフが呼ばれたタイミングでデータ取得を開始するなどに便利です。エージェントコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
- `input_type`: ハンドオフが受け取る入力の型 (任意)。
- `input_filter`: 次のエージェントが受け取る入力をフィルタリングします。詳細は後述します。

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

場合によっては、LLM がハンドオフを呼び出す際にデータを渡すことが必要です。たとえば「Escalation agent」へのハンドオフでは、理由を渡してログに残したいかもしれません。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴全体を閲覧できます。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定します。入力フィルターは [`HandoffInputData`][agents.handoffs.HandoffInputData] を受け取り、新しい `HandoffInputData` を返す関数です。

よくあるパターン (たとえば履歴からすべてのツール呼び出しを削除する) は [`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより、`FAQ agent` が呼び出されたときに履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるように、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨の接頭辞が用意されているほか、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出してプロンプトに推奨情報を自動的に追加することもできます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```