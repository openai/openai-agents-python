---
search:
  exclude: true
---
# Handoffs

Handoffs は、エージェントが別のエージェントにタスクを委譲できるようにします。これは、異なるエージェントがそれぞれ異なる領域に特化しているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、 FAQ などのタスクを個別に担当するエージェントがいるかもしれません。

Handoffs は  LLM  にはツールとして表現されます。そのため、`Refund Agent` という名前のエージェントへハンドオフする場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、これは直接 `Agent` を受け取るか、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取れます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先のエージェントを指定し、任意のオーバーライドや入力フィルターを設定できます。

### 基本的な使い方

シンプルなハンドオフの作り方は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使う方法（`billing_agent` のように）と、`handoff()` 関数を使う方法のどちらでも構いません。

### `handoff()` 関数による handoffs のカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数でカスタマイズできます。

-   `agent`: ハンドオフ先のエージェントです。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` 関数が使われ、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが起動されることが分かった時点でデータ取得を開始する、といった用途に便利です。この関数はエージェントのコンテキストを受け取り、任意で  LLM  が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: ハンドオフが受け取る想定の入力の型（任意）。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は下記を参照してください。

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

状況によっては、ハンドオフを呼び出す際に  LLM  からいくつかのデータを提供してほしいことがあります。たとえば、「エスカレーション エージェント」へのハンドオフを想定してください。記録のために理由を受け取りたい、ということがあるでしょう。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴全体を閲覧できるかのように振る舞います。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] を介して既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

よくあるパターン（たとえば履歴からすべてのツール呼び出しを取り除くなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. `FAQ agent` が呼び出されたときに、履歴からすべてのツールを自動的に削除します。

## 推奨プロンプト

LLMs が handoffs を正しく理解できるように、エージェントに handoffs に関する情報を含めることをおすすめします。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスが用意されています。あるいは、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトへ自動追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```