---
search:
  exclude: true
---
# ハンドオフ

ハンドオフは、ある エージェント から別の エージェント へタスクを委譲するための仕組みです。これは、異なる エージェント がそれぞれ固有の分野に特化しているシナリオで特に有用です。例えば、カスタマーサポートアプリでは、注文状況、返金、FAQ などを個別に担当する エージェント を用意できます。

ハンドオフは LLM に対してツールとして表現されます。例えば `Refund Agent` へのハンドオフがある場合、そのツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべての エージェント は、[`handoffs`][agents.agent.Agent.handoffs] パラメーターを持ち、これは直接 `Agent` を渡すことも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

Agents SDK によって提供される [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、引き渡し先の エージェント の指定に加え、オーバーライドや入力フィルターを任意で指定できます。

### 基本的な使い方

シンプルなハンドオフの作成方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接渡すことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では各種カスタマイズが可能です。

- `agent`: ハンドオフ先の エージェント。
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
- `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明文を上書きします。
- `on_handoff`: ハンドオフ実行時に呼び出されるコールバック関数。ハンドオフが呼ばれると分かった瞬間にデータ取得を開始する、といった用途に役立ちます。この関数はエージェントのコンテキストを受け取り、任意で LLM 生成の入力も受け取れます。入力データは `input_type` パラメーターで制御します。
- `input_type`: ハンドオフが想定する入力の型（任意）。
- `input_filter`: 次の エージェント が受け取る入力をフィルタリングします。詳細は後述します。
- `is_enabled`: ハンドオフを有効にするかどうか。ブール値またはブール値を返す関数を指定でき、実行時に動的に有効・無効を切り替えられます。

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

状況によっては、ハンドオフ呼び出し時に LLM に何らかのデータを提供してほしい場合があります。例えば「エスカレーション エージェント」へのハンドオフを想定すると、記録のために理由を一緒に渡したいことがあるでしょう。

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

ハンドオフが発生すると、新しい エージェント が会話を引き継ぎ、以前の会話履歴全体を参照できるようになります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] として受け取り、新しい `HandoffInputData` を返す関数です。

既定では、Runner はそれまでの発話を単一の assistant の要約メッセージに折りたたむようになりました（[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を参照）。この要約は、同一の実行中に複数回のハンドオフが起きた際に新しいターンを追記していく `<CONVERSATION HISTORY>` ブロック内に表示されます。完全な `input_filter` を書かずに生成メッセージだけを置き換えたい場合は、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] で独自のマッピング関数を指定できます。この既定は、ハンドオフ側と実行側のどちらも明示的な `input_filter` を提供しない場合にのみ適用されるため、既にペイロードをカスタマイズしているコード（このリポジトリの code examples を含む）は変更なしで現在の動作を維持します。単一のハンドオフに対してのみ入れ子の挙動を上書きしたい場合は、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡すことで、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定できます。生成された要約のラッパーテキストだけを変更したい場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] も）を呼び出してください。

よくあるパターン（例えば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これは、`FAQ agent` が呼び出された際に履歴からすべてのツールを自動的に削除します。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるように、エージェント内にハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出すことで、推奨情報をプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```