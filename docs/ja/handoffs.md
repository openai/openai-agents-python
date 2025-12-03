---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使うと、ある エージェント が別の エージェント にタスクを委譲できます。これは、異なる エージェント がそれぞれ固有の分野を専門とするシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクを個別に処理する エージェント がいるかもしれません。

ハンドオフは LLM に対してはツールとして表現されます。たとえば `Refund Agent` という エージェント へのハンドオフがある場合、そのツール名は `transfer_to_refund_agent` となります。

## ハンドオフの作成

すべての エージェント には [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、これは `Agent` を直接渡すことも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先の エージェント を指定し、さらにオプションの上書きや入力フィルターを指定できます。

### 基本的な使い方

シンプルなハンドオフの作り方は次のとおりです:

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接使うことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使うと、さまざまな点をカスタマイズできます。

-   `agent`: ハンドオフ先の エージェント です。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` からの既定のツール説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されることがわかった時点でデータ取得を開始するなどの用途に便利です。この関数は エージェント のコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: ハンドオフで想定される入力の型（オプション）。
-   `input_filter`: 次の エージェント が受け取る入力をフィルタリングできます。詳細は下記を参照してください。
-   `is_enabled`: ハンドオフが有効かどうか。ブール値、またはブール値を返す関数を指定でき、実行時に動的にハンドオフを有効・無効化できます。

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

状況によっては、ハンドオフ呼び出し時に LLM にデータの提供を求めたいことがあります。たとえば「エスカレーション エージェント」へのハンドオフを想定してください。ログのために理由を提供してもらいたいかもしれません。

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

ハンドオフが行われると、新しい エージェント が会話を引き継ぎ、以前の会話履歴全体を閲覧できる状態になります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

既定では、Runner は前の書き起こしを 1 つのアシスタント要約メッセージに折りたたむようになりました（[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を参照）。この要約は、同じ実行中に複数回のハンドオフが発生した場合に新しいターンを追加し続ける `<CONVERSATION HISTORY>` ブロック内に表示されます。完全な `input_filter` を書かずに生成されたメッセージを置き換えたい場合は、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を指定して独自のマッピング関数を提供できます。この既定は、ハンドオフ側と実行側のどちらも明示的な `input_filter` を提供しない場合にのみ適用されるため、ペイロードをすでにカスタマイズしている既存のコード（このリポジトリの code examples を含む）は、変更なしで現在の動作を維持します。単一のハンドオフに対してネストの挙動を上書きしたい場合は、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡して、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定します。生成された要約のラッパーテキストだけを変更する必要がある場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

いくつかの一般的なパターン（たとえば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これは、`FAQ agent` が呼び出されたときに履歴からすべてのツールを自動的に削除します。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるように、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```