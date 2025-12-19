---
search:
  exclude: true
---
# ハンドオフ

ハンドオフにより、ある エージェント が別の エージェント にタスクを委譲できます。これは、異なる エージェント がそれぞれ異なる分野に特化しているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などをそれぞれ専担当で処理する エージェント が存在し得ます。

ハンドオフは LLM に対してはツールとして表現されます。たとえば `Refund Agent` という エージェント へのハンドオフがある場合、そのツールは `transfer_to_refund_agent` と呼ばれます。

## ハンドオフの作成

すべての エージェント は [`handoffs`][agents.agent.Agent.handoffs] パラメーターを持ち、これは `Agent` を直接渡すことも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、引き渡し先の エージェント に加え、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使い方

シンプルなハンドオフの作り方は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接使うことも、`handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では以下の点をカスタマイズできます。

- `agent`: 引き渡し先の エージェント です。
- `tool_name_override`: 既定では `Handoff.default_tool_name()` が使用され、`transfer_to_<agent_name>` に解決されます。これを上書きできます。
- `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明を上書きします。
- `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼ばれたことが分かった時点でデータ取得を開始する、といった用途に便利です。この関数は エージェント のコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
- `input_type`: ハンドオフが受け取る想定の入力タイプ（任意）。
- `input_filter`: 次の エージェント が受け取る入力をフィルタリングします。詳細は以下を参照してください。
- `is_enabled`: ハンドオフを有効にするかどうか。ブール値、またはブール値を返す関数を指定でき、実行時に動的に有効・無効を切り替えられます。

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

状況によっては、ハンドオフを呼び出す際に LLM によるデータ提供が必要な場合があります。たとえば「エスカレーション エージェント」へのハンドオフを想定すると、ログのために理由を提供してほしいといったケースです。

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

ハンドオフが発生すると、新しい エージェント が会話を引き継ぎ、直前までの会話履歴全体を参照できるかのように振る舞います。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] を介して既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

デフォルトでは、Runner は直前のトランスクリプトを 1 件の assistant サマリーメッセージに折りたたみます（[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を参照）。このサマリーは、同一の実行中に複数回のハンドオフが起きた場合に新しいターンが追記され続ける `<CONVERSATION HISTORY>` ブロック内に表示されます。完全な `input_filter` を書かずに生成メッセージを置き換えたい場合は、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を指定して独自のマッピング関数を提供できます。これは、ハンドオフ側と実行側のどちらも明示的な `input_filter` を提供していない場合にのみ適用されるため、既存コード（このリポジトリの code examples を含む）で既にペイロードをカスタマイズしているものは、変更なしで現在の挙動が維持されます。単一のハンドオフについてネスティングの挙動を上書きするには、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡して、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定してください。生成サマリーのラッパーテキストだけを変更したい場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] も）を呼び出してください。

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

1. これは、`FAQ agent` が呼び出されたときに履歴から自動的にすべてのツールを削除します。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるようにするため、 エージェント にハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨の接頭辞が用意されています。あるいは、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```