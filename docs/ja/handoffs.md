---
search:
  exclude: true
---
# ハンドオフ

ハンドオフは、あるエージェントが別のエージェントにタスクを委譲できる仕組みです。これは、異なるエージェントがそれぞれ異なる分野に特化しているシナリオで特に有用です。たとえば、カスタマーサポートアプリには、注文状況、返金、FAQ などのタスクを個別に担当するエージェントがいる場合があります。

ハンドオフは LLM に対してツールとして表現されます。たとえば、`Refund Agent` という名前のエージェントへのハンドオフがある場合、そのツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントは [`handoffs`][agents.agent.Agent.handoffs] パラメーターを持ち、これは `Agent` を直接渡すか、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すことができます。

プレーンな `Agent` インスタンスを渡す場合、その [`handoff_description`][agents.agent.Agent.handoff_description]（設定されているとき）はデフォルトのツール説明に追加されます。完全な `handoff()` オブジェクトを書かずに、モデルがそのハンドオフを選ぶべきタイミングを示すために使用します。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、引き継ぎ先のエージェントに加えて、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

シンプルなハンドオフの作成方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のようにエージェントを直接使うことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では各種カスタマイズが可能です。

- `agent`: 引き継ぎ先のエージェントです。
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
- `tool_description_override`: `Handoff.default_tool_description()` によるデフォルトのツール説明を上書きします。
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが実行されることが分かった時点でデータ取得を開始する、といった用途に有用です。この関数はエージェントコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
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

## ハンドオフの入力

状況によっては、ハンドオフを呼び出す際に LLM にいくつかのデータを提供してほしいことがあります。たとえば、「エスカレーションエージェント」へのハンドオフを想定してください。ログ記録のために理由を渡したい場合があります。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、これまでの会話履歴全体を閲覧できるかのように振る舞います。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] として受け取り、新しい `HandoffInputData` を返す関数です。

デフォルトでは、ランナーは直前までの発話録を 1 件の assistant の要約メッセージに折りたたみます（[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を参照）。この要約は、同一の実行内で複数回のハンドオフが起きる場合に新しいターンを追記していく `<CONVERSATION HISTORY>` ブロックの中に現れます。[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を指定すると、完全な `input_filter` を書かずに生成メッセージを置き換えるための独自マッピング関数を提供できます。なお、このデフォルトは、ハンドオフ側と実行側のどちらにも明示的な `input_filter` がない場合にのみ適用されるため、既にペイロードをカスタマイズしているコード（このリポジトリの code examples を含む）は変更なしで現行の挙動を維持します。単一のハンドオフについてネスト動作を上書きしたい場合は、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡してください。これは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定します。生成された要約のラッパーテキストだけを変更したい場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] も）を呼び出してください。

一般的なパターン（たとえば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

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

LLM がハンドオフを正しく理解できるようにするため、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、あるいは [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```