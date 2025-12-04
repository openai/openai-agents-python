---
search:
  exclude: true
---
# ハンドオフ

ハンドオフにより、ある エージェント が別の エージェント にタスクを委譲できます。これは、異なる エージェント が各分野に特化しているシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ担当する エージェント が存在するかもしれません。

ハンドオフは LLM に対してツールとして表現されます。たとえば、`Refund Agent` という エージェント へのハンドオフがある場合、そのツールは `transfer_to_refund_agent` という名称になります。

## ハンドオフの作成

すべての エージェント には [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接渡すか、ハンドオフをカスタマイズする `Handoff` オブジェクトを渡せます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使ってハンドオフを作成できます。この関数では、引き渡し先の エージェント の指定に加えて、任意の上書き設定や入力フィルターを指定できます。

### 基本的な使い方

簡単なハンドオフの作成方法は次のとおりです。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. `billing_agent` のように エージェント を直接使う方法と、`handoff()` 関数を使う方法があります。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数では各種カスタマイズが可能です。

-   `agent`: 引き渡し先の エージェント です。
-   `tool_name_override`: 既定では `Handoff.default_tool_name()` 関数が使われ、`transfer_to_<agent_name>` が生成されます。これを上書きできます。
-   `tool_description_override`: `Handoff.default_tool_description()` による既定のツール説明を上書きします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼ばれたことが分かった時点でデータ取得を開始する、といった用途に便利です。この関数はエージェントコンテキストを受け取り、必要に応じて LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御します。
-   `input_type`: ハンドオフが想定する入力の型（任意）です。
-   `input_filter`: 次の エージェント が受け取る入力をフィルタリングできます。詳細は以下をご覧ください。
-   `is_enabled`: ハンドオフを有効にするかどうかです。真偽値、または真偽値を返す関数を指定でき、実行時に動的に有効・無効を切り替えられます。

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

状況によっては、ハンドオフ呼び出し時に LLM にデータの提供を求めたい場合があります。たとえば「エスカレーション エージェント」へのハンドオフでは、記録のために理由を提供してほしい、といったケースです。

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

ハンドオフが行われると、新しい エージェント が会話を引き継ぎ、以前の会話履歴全体を閲覧できるようになります。これを変更したい場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] として既存の入力を受け取り、新しい `HandoffInputData` を返す関数です。

既定では、runner は直前までのトランスクリプトを 1 つのアシスタント要約メッセージに折りたたみます（[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を参照）。この要約は、同一の実行中に複数のハンドオフが発生した場合でも新しいターンを追記していく、`<CONVERSATION HISTORY>` ブロック内に表示されます。完全な `input_filter` を書かなくても、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を使って生成メッセージを置き換えるマッピング関数を提供できます。この既定は、ハンドオフ側と実行側のどちらにも明示的な `input_filter` が指定されていない場合にのみ適用されます。したがって、既存のペイロードをすでにカスタマイズしているコード（このリポジトリの code examples を含む）は、変更なしで現在の動作を維持します。単一のハンドオフについて入れ子の挙動を上書きしたい場合は、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡して、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] を設定します。生成された要約のラッパーテキストだけを変更したい場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください（必要に応じて [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] も）。

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

1. これは、`FAQ agent` が呼び出されたときに履歴からすべてのツールを自動的に削除します。

## 推奨プロンプト

LLM がハンドオフを正しく理解できるように、エージェント内にハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨のプレフィックスがあり、または [`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨データをプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```