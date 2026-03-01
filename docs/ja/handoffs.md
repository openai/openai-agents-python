---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使うと、エージェントが別のエージェントにタスクを委任できます。これは、異なるエージェントがそれぞれ別の領域を専門にしているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、 FAQ などのタスクをそれぞれ専門に処理するエージェントを用意できます。

ハンドオフは LLM に対してツールとして表現されます。そのため、`Refund Agent` という名前のエージェントへのハンドオフがある場合、そのツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接渡すことも、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡すこともできます。

プレーンな `Agent` インスタンスを渡すと、[`handoff_description`][agents.agent.Agent.handoff_description]（設定されている場合）がデフォルトのツール説明に追加されます。これを使うと、完全な `handoff()` オブジェクトを書かなくても、そのハンドオフをモデルが選ぶべきタイミングを示せます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先エージェントに加えて、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使用方法

シンプルなハンドオフの作成方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使用することも（`billing_agent` のように）、`handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使うと、さまざまな項目をカスタマイズできます。

-   `agent`: これは、ハンドオフ先のエージェントです。
-   `tool_name_override`: デフォルトでは `Handoff.default_tool_name()` 関数が使われ、`transfer_to_<agent_name>` に解決されます。これをオーバーライドできます。
-   `tool_description_override`: `Handoff.default_tool_description()` のデフォルトのツール説明をオーバーライドします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。これは、ハンドオフ呼び出しが行われるとわかった時点でデータ取得を開始する、といった用途に有用です。この関数はエージェントコンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御されます。
-   `input_type`: ハンドオフが期待する入力の型です（任意）。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は以下をご覧ください。
-   `is_enabled`: ハンドオフを有効にするかどうかです。これは真偽値、または真偽値を返す関数を指定でき、実行時に動的にハンドオフを有効化または無効化できます。
-   `nest_handoff_history`: RunConfig レベルの `nest_handoff_history` 設定に対する、呼び出しごとの任意オーバーライドです。`None` の場合は、アクティブな実行設定で定義された値が代わりに使われます。

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

特定の状況では、ハンドオフ呼び出し時に LLM からデータを提供させたい場合があります。たとえば「Escalation agent」へのハンドオフを想像してください。ログ記録のために理由を提供してほしい場合があります。

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

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、それまでの会話履歴全体を参照できる状態になります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

ネストされたハンドオフはオプトインの beta として提供されており、安定化のためデフォルトでは無効です。[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を有効にすると、ランナーは以前のトランスクリプトを単一のアシスタント要約メッセージにまとめ、`<CONVERSATION HISTORY>` ブロックでラップします。このブロックは、同一実行中に複数のハンドオフが発生した場合に新しいターンを追記し続けます。生成されるメッセージを完全な `input_filter` を書かずに置き換えたい場合は、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] で独自のマッピング関数を指定できます。このオプトインは、ハンドオフ側と実行側のどちらにも明示的な `input_filter` が指定されていない場合にのみ適用されるため、すでにペイロードをカスタマイズしている既存コード（このリポジトリのコード例を含む）は変更なしで現在の動作を維持します。単一のハンドオフについてネスト動作を上書きするには、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡します。これは [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定します。生成される要約のラッパーテキストだけを変更したい場合は、エージェント実行前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] も）を呼び出してください。

いくつかの一般的なパターン（たとえば履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装されています。

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

LLM がハンドオフを適切に理解できるようにするため、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨プレフィックスが用意されており、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して推奨データをプロンプトに自動追加することもできます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```