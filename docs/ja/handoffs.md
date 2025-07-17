---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、ある エージェント がタスクを別の エージェント に委譲できます。これは、異なる エージェント がそれぞれ異なる分野を専門としているシナリオで特に有用です。たとえば、カスタマーサポート アプリでは、注文状況、返金、FAQ などのタスクをそれぞれ担当する エージェント が存在するかもしれません。

ハンドオフは LLM からはツールとして表現されます。したがって `Refund Agent` という名前の エージェント へのハンドオフがある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべての エージェント には [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、直接 `Agent` を渡すことも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使用してハンドオフを作成できます。この関数では、ハンドオフ先の エージェント に加えて、オーバーライドや入力フィルターなどのオプションを指定できます。

### 基本的な使い方

以下はシンプルなハンドオフの作成例です。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接渡すこともできますし、`handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、次のようなカスタマイズが可能です。

-   `agent`: ハンドオフ先となる エージェント です。  
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` という名前になります。これを上書きできます。  
-   `tool_description_override`: `Handoff.default_tool_description()` によるデフォルトのツール説明を上書きします。  
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが発生した時点でデータ取得を開始する、といった用途に便利です。この関数は エージェント コンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。  
-   `input_type`: ハンドオフで想定される入力の型（任意）。  
-   `input_filter`: 次の エージェント が受け取る入力をフィルタリングできます。詳細は後述します。  

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

場合によっては、 LLM にハンドオフ呼び出し時にいくつかのデータを渡してほしいことがあります。たとえば「Escalation agent」へのハンドオフでは、その理由を一緒に受け取りログに残したい、というケースです。

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

ハンドオフが発生すると、新しい エージェント が会話を引き継ぎ、これまでの会話履歴全体を見ることができます。これを変更したい場合は [`input_filter`][agents.handoffs.Handoff.input_filter] を設定します。入力フィルターは [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

よくあるパターン（たとえば履歴からすべてのツール呼び出しを取り除くなど）は [`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより `FAQ agent` が呼び出された際、履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるように、 エージェント にハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスが用意されているほか、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出してプロンプトに推奨データを自動で追加することもできます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```