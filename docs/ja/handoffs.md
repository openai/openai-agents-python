---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使うと、エージェントが別のエージェントにタスクを委譲できます。これは、異なるエージェントがそれぞれ別分野を専門とするシナリオで特に有用です。たとえば、カスタマーサポート アプリでは、注文状況、返金、 FAQ などのタスクをそれぞれ専用に処理するエージェントを持てます。

ハンドオフは LLM に対してツールとして表現されます。したがって、 `Refund Agent` という名前のエージェントへのハンドオフがある場合、そのツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、 `Agent` を直接渡すか、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡せます。

プレーンな `Agent` インスタンスを渡すと、（設定されている場合）その [`handoff_description`][agents.agent.Agent.handoff_description] がデフォルトのツール説明に追記されます。完全な `handoff()` オブジェクトを書かずに、モデルがそのハンドオフを選ぶべきタイミングを示すヒントとして使ってください。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加えて、任意のオーバーライドや input filter を指定できます。

### 基本的な使い方

シンプルなハンドオフは次のように作成できます。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接（ `billing_agent` のように）使うことも、 `handoff()` 関数を使うこともできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では、さまざまな点をカスタマイズできます。

-   `agent`: ハンドオフ先のエージェントです。
-   `tool_name_override`: デフォルトでは `Handoff.default_tool_name()` 関数が使われ、 `transfer_to_<agent_name>` に解決されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` のデフォルト ツール説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。これは、ハンドオフが呼び出されるとわかった時点ですぐにデータ取得を開始する、といった用途に役立ちます。この関数はエージェント コンテキストを受け取り、任意で LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御されます。
-   `input_type`: ハンドオフが期待する入力の型です（任意）。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は以下を参照してください。
-   `is_enabled`: ハンドオフを有効にするかどうかです。これは boolean または boolean を返す関数にでき、実行時にハンドオフを動的に有効化または無効化できます。
-   `nest_handoff_history`: RunConfig レベルの `nest_handoff_history` 設定に対する、呼び出しごとの任意の上書きです。 `None` の場合は、アクティブな実行設定で定義された値が代わりに使用されます。

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

状況によっては、ハンドオフを呼び出すときに LLM に何らかのデータを提供させたい場合があります。たとえば「 Escalation agent 」へのハンドオフを考えてみてください。ログ記録のために、理由を提供してほしい場合があります。

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

## Input filters

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、それまでの会話履歴全体を参照できる状態になります。これを変更したい場合は、 [`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。 input filter は、既存の入力を [`HandoffInputData`][agents.handoffs.HandoffInputData] 経由で受け取り、新しい `HandoffInputData` を返す関数です。

ネストされたハンドオフはオプトインの beta として利用可能で、安定化のためデフォルトでは無効です。[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を有効にすると、 runner はそれ以前の transcript を 1 つの assistant 要約メッセージにまとめ、同一 run 中に複数のハンドオフが発生した場合に新しいターンを追加し続ける `<CONVERSATION HISTORY>` ブロックでラップします。完全な `input_filter` を書かなくても、 [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を通じて独自のマッピング関数を提供し、生成メッセージを置き換えられます。このオプトインは、ハンドオフ側にも run 側にも明示的な `input_filter` がない場合にのみ適用されるため、すでにペイロードをカスタマイズしている既存コード（このリポジトリのコード例を含む）は変更なしで現在の動作を維持します。 [`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡して [`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定することで、単一のハンドオフに対してネスト動作を上書きできます。生成される要約のラッパー テキストだけを変更したい場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers]（および必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers]）を呼び出してください。

いくつかの一般的なパターン（たとえば履歴からすべてのツール呼び出しを削除するなど）は、 [`agents.extensions.handoff_filters`][] に実装済みです。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより、 `FAQ agent` が呼び出されたときに履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

LLM がハンドオフを適切に理解できるようにするため、エージェントにハンドオフに関する情報を含めることを推奨します。推奨プレフィックスとして [`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] を用意しています。あるいは、 [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動追加することもできます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```