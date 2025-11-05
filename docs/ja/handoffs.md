---
search:
  exclude: true
---
# ハンドオフ

ハンドオフは、ある エージェント が別の エージェント にタスクを委譲できるようにする仕組みです。これは、異なる エージェント がそれぞれ異なる分野を専門としている状況で特に有用です。例えば、カスタマーサポートアプリでは、注文状況、返金、FAQ などをそれぞれ担当する エージェント がいるかもしれません。

ハンドオフは LLM に対してツールとして表現されます。たとえば `Refund Agent` という名前の エージェント へのハンドオフがある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべての エージェント は [`handoffs`][agents.agent.Agent.handoffs] パラメーターを持ち、これは直接 `Agent` を受け取るか、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ります。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先の エージェント に加えて、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

シンプルなハンドオフの作成方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接使うことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数で各種カスタマイズが可能です。

-   `agent`: ハンドオフ先の エージェント です。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` 関数が使われ、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` の既定ツール説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼ばれたタイミングでデータ取得を開始するなどに便利です。この関数は エージェント コンテキストを受け取り、任意で LLM 生成の入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: ハンドオフが想定する入力の型（任意）。
-   `input_filter`: 次の エージェント が受け取る入力をフィルタリングできます。詳細は以下を参照してください。
-   `is_enabled`: ハンドオフを有効にするかどうか。真偽値、または真偽値を返す関数を指定でき、実行時に動的に有効/無効を切り替えられます。

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

状況によっては、ハンドオフを呼び出す際に LLM に何らかのデータを提供してほしい場合があります。例えば「エスカレーション エージェント」へのハンドオフを想像してみてください。記録のために理由を提供してほしい、ということがあるでしょう。

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

ハンドオフが起きると、新しい エージェント が会話を引き継ぎ、これまでの会話履歴全体を閲覧できるかのように振る舞います。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] として受け取り、新しい `HandoffInputData` を返す関数です。

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

1. これにより、`FAQ agent` が呼び出されたときに履歴からツールが自動的にすべて削除されます。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるように、エージェント にハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあります。あるいは [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データを自動的にプロンプトへ追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```