---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、エージェントはタスクを別のエージェントに委任できます。これは、異なるエージェントがそれぞれ異なる領域に特化しているシナリオで特に役立ちます。たとえば、カスタマーサポートアプリでは、注文状況、返金、よくある質問などのタスクを、それぞれ専任のエージェントが処理できます。

ハンドオフは、LLM に対してツールとして表現されます。そのため、`Refund Agent` という名前のエージェントへのハンドオフがある場合、そのツールは `transfer_to_refund_agent` と呼ばれます。

## ハンドオフの作成

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接受け取ることも、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ることもできます。

`Agent` インスタンスをそのまま渡した場合、その [`handoff_description`][agents.agent.Agent.handoff_description] が設定されていれば、デフォルトのツール説明に追加されます。完全な `handoff()` オブジェクトを記述せずに、モデルがそのハンドオフを選択すべきタイミングを示すために使用できます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使用して、ハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加えて、任意のオーバーライドや入力フィルターを指定できます。

### 基本的な使用法

簡単なハンドオフは、次のように作成できます。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使用することも（`billing_agent` のように）、`handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ

[`handoff()`][agents.handoffs.handoff] 関数を使用すると、さまざまな項目をカスタマイズできます。

-   `agent`: ハンドオフ先となるエージェントです。
-   `tool_name_override`: デフォルトでは `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` に解決されます。これはオーバーライドできます。
-   `tool_description_override`: `Handoff.default_tool_description()` のデフォルトのツール説明をオーバーライドします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されることが判明した時点で、データ取得を開始する場合などに役立ちます。この関数はエージェントコンテキストを受け取り、必要に応じて LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターによって制御されます。
-   `input_type`: ハンドオフのツール呼び出し引数のスキーマです。設定すると、解析済みのペイロードが `on_handoff` に渡されます。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細は以下を参照してください。
-   `is_enabled`: ハンドオフが有効かどうかを指定します。真偽値、または真偽値を返す関数を指定でき、実行時にハンドオフを動的に有効化または無効化できます。
-   `nest_handoff_history`: `RunConfig` レベルの `nest_handoff_history` 設定を呼び出し単位でオーバーライドする任意の設定です。`None` の場合は、代わりにアクティブな実行設定で定義されている値が使用されます。

[`handoff()`][agents.handoffs.handoff] ヘルパーは、渡された特定の `agent` に常に制御を移します。複数の移行先が考えられる場合は、移行先ごとにハンドオフを 1 つ登録し、モデルに選択させてください。呼び出し時にどのエージェントを返すかを独自のハンドオフコードで決定する必要がある場合にのみ、カスタムの [`Handoff`][agents.handoffs.Handoff] を使用してください。

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

状況によっては、LLM がハンドオフを呼び出す際に、何らかのデータを提供するようにしたい場合があります。たとえば、「エスカレーションエージェント」へのハンドオフを想定します。ログに記録できるよう、モデルに理由を提供させることができます。

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

`input_type` は、ハンドオフのツール呼び出し自体の引数を記述します。SDK はそのスキーマをハンドオフツールの `parameters` としてモデルに公開し、返された JSON をローカルで検証して、解析済みの値を `on_handoff` に渡します。

これは次のエージェントのメイン入力を置き換えるものではなく、別の移行先を選択するものでもありません。[`handoff()`][agents.handoffs.handoff] ヘルパーは引き続き、ラップした特定のエージェントに制御を移し、受け取る側のエージェントは、[`input_filter`][agents.handoffs.Handoff.input_filter] またはネストされたハンドオフ履歴の設定で変更しない限り、引き続き会話履歴を参照できます。

`input_type` は [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] とも別のものです。`input_type` は、すでにローカルに存在するアプリケーションの状態や依存関係ではなく、ハンドオフ時にモデルが決定するメタデータに使用してください。

### `input_type` の使用場面

ハンドオフに `reason`、`language`、`priority`、`summary` など、モデルが生成する少量のメタデータが必要な場合は、`input_type` を使用します。たとえば、トリアージエージェントは `{ "reason": "duplicate_charge", "priority": "high" }` を指定して返金エージェントにハンドオフでき、返金エージェントが引き継ぐ前に、`on_handoff` でそのメタデータをログに記録したり永続化したりできます。

目的が異なる場合は、別の仕組みを選択してください。

-   既存のアプリケーションの状態と依存関係は、[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] に格納します。[コンテキストガイド](context.md)を参照してください。
-   受け取る側のエージェントが参照する履歴を変更する場合は、[`input_filter`][agents.handoffs.Handoff.input_filter]、[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]、または [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を使用します。
-   複数の専門エージェントが移行先の候補となる場合は、移行先ごとにハンドオフを 1 つ登録します。`input_type` は選択されたハンドオフにメタデータを追加できますが、移行先を振り分けるものではありません。
-   会話を移行せず、ネストされた専門エージェントに構造化された入力を渡す場合は、[`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool] を使用することを推奨します。[ツール](tools.md#structured-input-for-tool-agents)を参照してください。

## 入力フィルター

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、それまでの会話履歴全体を参照できるようになります。これを変更する場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] を介して既存の入力を受け取り、新しい `HandoffInputData` を返す必要がある関数です。

[`HandoffInputData`][agents.handoffs.HandoffInputData] には、以下が含まれます。

-   `input_history`: `Runner.run(...)` が開始される前の入力履歴です。
-   `pre_handoff_items`: ハンドオフが呼び出されたエージェントターンより前に生成された項目です。
-   `new_items`: ハンドオフ呼び出しとハンドオフ出力項目を含む、現在のターン中に生成された項目です。
-   `input_items`: `new_items` の代わりに次のエージェントへ転送する任意の項目です。セッション履歴では `new_items` をそのまま維持しながら、モデル入力をフィルタリングできます。
-   `run_context`: ハンドオフが呼び出された時点でアクティブな [`RunContextWrapper`][agents.run_context.RunContextWrapper] です。

ネストされたハンドオフは、オプトインのベータ機能として利用できますが、安定化を進めている間はデフォルトで無効になっています。[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を有効にすると、ランナーは要約可能な履歴を順序付けられたアシスタント要約セグメントに圧縮する一方で、情報を失わないメッセージ項目を元の位置に保持します。生成される各要約セグメントでは `<CONVERSATION HISTORY>` ラッパーが使用され、後続のハンドオフでは、順序付きの会話記録を再構築する前に、以前に生成されたセグメントがフラット化されます。セッション、`RunState`、および `RunResult.to_input_list()` は、この SDK のデフォルト履歴に移されたメッセージの各出現を正確に追跡するため、それらが二重に追加されることはありません。一方、内容が同一でも別個のメッセージは引き続き保持されます。組み込みのセグメント化を使用せず、次のエージェントに渡す入力項目の正確なリストを返す独自のマッピング関数を、[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] で指定できます。このオプトイン設定は、ハンドオフと実行のどちらにも明示的な `input_filter` が指定されていない場合にのみ適用されます。そのため、このリポジトリ内のコード例を含め、ペイロードをすでにカスタマイズしている既存のコードでは、変更せずに現在の動作が維持されます。単一のハンドオフに対してネスト動作をオーバーライドするには、[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡します。これにより、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] が設定されます。生成される要約セグメントのラッパーテキストのみを変更する場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します。必要に応じて、[`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] も呼び出せます。

ハンドオフとアクティブな [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] の両方でフィルターが定義されている場合、その特定のハンドオフでは、ハンドオフ単位の [`input_filter`][agents.handoffs.Handoff.input_filter] が優先されます。

!!! note

    ハンドオフは単一の実行内で行われます。入力ガードレールは引き続きチェーン内の最初のエージェントにのみ適用され、出力ガードレールは最終出力を生成するエージェントにのみ適用されます。ワークフロー内の各カスタム関数ツール呼び出しをチェックする必要がある場合は、ツールガードレールを使用してください。

履歴からすべてのツール呼び出しを削除するなど、いくつかの一般的なパターンがあり、[`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. これにより、`FAQ agent` が呼び出されたときに、履歴からすべてのツールが自動的に削除されます。

## 推奨プロンプト

LLM がハンドオフを適切に理解できるよう、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨プレフィックスが用意されています。または、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出して、推奨情報をプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```