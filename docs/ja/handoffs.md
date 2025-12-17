---
search:
  exclude: true
---
# ハンドオフ

ハンドオフは、あるエージェントが別のエージェントにタスクを委譲できるようにするものです。これは、異なるエージェントがそれぞれ異なる分野を専門としている状況で特に有用です。例えば、カスタマーサポート アプリでは、注文状況、返金、FAQ などのタスクをそれぞれ専任で扱うエージェントがいるかもしれません。

ハンドオフは、 LLM に対してはツールとして表現されます。例えば、`Refund Agent` というエージェントへのハンドオフがある場合、そのツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、これは `Agent` を直接受け取ることも、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ることもできます。

Agents SDK によって提供される [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、委譲先のエージェントに加えて、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

以下は、シンプルなハンドオフの作り方です。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使う（`billing_agent` のように）ことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、さまざまなカスタマイズが可能です。

- `agent`: ハンドオフ先のエージェントです。
- `tool_name_override`: 既定では `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
- `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明を上書きします。
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。これは、ハンドオフが呼び出されると分かった時点でデータ取得を開始するなどに便利です。この関数はエージェントのコンテキストを受け取り、オプションで LLM 生成の入力も受け取れます。入力データは `input_type` パラメーターで制御します。
- `input_type`: ハンドオフが想定する入力の型（任意）。
- `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は以下を参照してください。
- `is_enabled`: ハンドオフを有効にするかどうか。真偽値、または真偽値を返す関数を指定でき、実行時に動的に有効・無効を切り替えられます。

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

状況によっては、ハンドオフを呼び出す際に LLM にいくらかのデータを提供してほしい場合があります。例えば、「エスカレーション エージェント」へのハンドオフを想定してください。理由を提供してもらい、それを記録したくなるかもしれません。

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

ハンドオフが行われると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴全体を参照できるようになります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] を介して既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

既定では、ランナーは以前のトランスクリプトを 1 件のアシスタント要約メッセージに折りたたみます（[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を参照）。この要約は、同じ実行中に複数のハンドオフが発生する場合に新しいターンが追記されていく `<CONVERSATION HISTORY>` ブロック内に表示されます。完全な `input_filter` を書かずに生成されたメッセージを置き換えるには、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を通じて独自のマッピング関数を提供できます。これは、ハンドオフ側と実行側のいずれも明示的な `input_filter` を提供しない場合にのみ適用される既定動作のため、すでにペイロードをカスタマイズしている既存のコード（このリポジトリの code examples を含む）は、変更なしで現在の動作を維持します。単一のハンドオフについてネスト動作を上書きしたい場合は、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡して [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定してください。生成される要約のラッパー文言だけを変更したい場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（および必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

いくつかの一般的なパターン（例えば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これは、`FAQ agent` が呼び出されたときに履歴から自動的にすべてのツールを削除します。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるようにするため、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスを用意しています。あるいは、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```