---
search:
  exclude: true
---
# ハンドオフ

ハンドオフにより、エージェントはタスクを別のエージェントに委任できます。これは、異なるエージェントがそれぞれ別の領域を専門とするシナリオで特に有用です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクをそれぞれ専門に処理するエージェントを用意できます。

ハンドオフは、LLM に対してツールとして表現されます。そのため、`Refund Agent` という名前のエージェントへのハンドオフがある場合、ツール名は `transfer_to_refund_agent` になります。

## ハンドオフの作成 {#creating-a-handoff}

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接受け取ることも、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ることもできます。

通常の `Agent` インスタンスを渡すと、その [`handoff_description`][agents.agent.Agent.handoff_description] が設定されている場合、デフォルトのツール説明に追加されます。完全な `handoff()` オブジェクトを作成せずに、モデルがそのハンドオフを選択すべきタイミングを示すために使用してください。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使用して、ハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加えて、オプションのオーバーライドや入力フィルターを指定できます。

### 基本的な使用法 {#basic-usage}

単純なハンドオフは、次のように作成できます。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使用することも（`billing_agent` のように）、`handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ {#customizing-handoffs-via-the-handoff-function}

[`handoff()`][agents.handoffs.handoff] 関数を使用すると、さまざまな要素をカスタマイズできます。

-   `agent`: ハンドオフ先となるエージェントです。
-   `tool_name_override`: デフォルトでは `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` に解決されます。これはオーバーライドできます。
-   `tool_description_override`: `Handoff.default_tool_description()` のデフォルトのツール説明をオーバーライドします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されることが判明した時点で、データ取得を開始する場合などに役立ちます。この関数はエージェントのコンテキストを受け取り、必要に応じて LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターによって制御されます。
-   `input_type`: ハンドオフのツール呼び出し引数のスキーマです。設定すると、解析済みのペイロードが `on_handoff` に渡されます。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳細については後述します。
-   `is_enabled`: ハンドオフが有効かどうかを指定します。ブール値、またはブール値を返す関数を指定でき、実行時にハンドオフを動的に有効化または無効化できます。
-   `nest_handoff_history`: RunConfig レベルの `nest_handoff_history` 設定に対する、ハンドオフごとのオプションのオーバーライドです。`None` の場合、代わりに有効な実行設定で定義された値が使用されます。

[`handoff()`][agents.handoffs.handoff] ヘルパーは、渡された特定の `agent` に常に制御を移します。複数の移行先が考えられる場合は、移行先ごとに 1 つのハンドオフを登録し、モデルにその中から選択させてください。独自のハンドオフコードで、呼び出し時にどのエージェントを返すかを決定する必要がある場合にのみ、カスタムの [`Handoff`][agents.handoffs.Handoff] を使用してください。

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

## ハンドオフ入力 {#handoff-inputs}

状況によっては、ハンドオフを呼び出す際に、LLM から何らかのデータを提供させたい場合があります。たとえば、「エスカレーション担当エージェント」へのハンドオフを考えてみましょう。ログに記録できるように、モデルから理由を提供させることができます。

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

`is_enabled` は、モデルがハンドオフ引数を返す前、SDK が利用可能なハンドオフを準備する際に評価されるため、引数を持つハンドオフ内の値を認可することはできません。認可が解析済みフィールドに依存する場合は、アプリケーション側で副作用が発生する前に、`on_handoff` の先頭で確認してください。認可に失敗した場合は、値を返すのではなく例外を送出してください。`on_handoff` が正常に返ると、SDK は移行を続行します。ツール入力ガードレールは関数ツールに適用され、ハンドオフには適用されません。

これは、次のエージェントのメイン入力を置き換えるものではなく、別の移行先を選択するものでもありません。[`handoff()`][agents.handoffs.handoff] ヘルパーは、引き続きラップした特定のエージェントに移行し、受信側のエージェントも、[`input_filter`][agents.handoffs.Handoff.input_filter] またはネストされたハンドオフ履歴の設定で変更しない限り、会話履歴を引き続き参照できます。

`input_type` は、[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] とも別のものです。`input_type` は、すでにローカルに存在するアプリケーション状態や依存関係ではなく、ハンドオフ時にモデルが決定するメタデータに使用してください。

### `input_type` の使用場面 {#when-to-use-input_type}

ハンドオフで、`reason`、`language`、`priority`、`summary` など、モデルが生成する少量のメタデータが必要な場合は、`input_type` を使用します。たとえば、トリアージエージェントは `{ "reason": "duplicate_charge", "priority": "high" }` を指定して返金担当エージェントにハンドオフでき、返金担当エージェントが引き継ぐ前に、`on_handoff` でそのメタデータをログに記録したり永続化したりできます。

目的が異なる場合は、別の仕組みを選択してください。

-   既存のアプリケーション状態と依存関係は、[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] に格納します。[コンテキストガイド](context.md)を参照してください。
-   受信側のエージェントに表示される履歴を変更する場合は、[`input_filter`][agents.handoffs.Handoff.input_filter]、[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]、または [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を使用します。
-   複数の専門エージェントが移行先の候補となる場合は、移行先ごとに 1 つのハンドオフを登録します。`input_type` は選択されたハンドオフにメタデータを追加できますが、移行先を振り分けることはできません。
-   会話を移行せずに、ネストされた専門エージェントへ構造化入力を渡す場合は、[`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool] の使用を推奨します。[ツール](tools.md#structured-input-for-tool-agents)を参照してください。

## 入力フィルター {#input-filters}

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、それまでの会話履歴全体を参照できるようになります。これを変更する場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] を介して既存の入力を受け取り、新しい `HandoffInputData` を返す必要がある関数です。

[`HandoffInputData`][agents.handoffs.HandoffInputData] には、次のものが含まれます。

-   `input_history`: `Runner.run(...)` が開始される前の入力履歴です。
-   `pre_handoff_items`: ハンドオフが呼び出されたエージェントターンより前に生成された項目です。
-   `new_items`: ハンドオフ呼び出しとハンドオフ出力項目を含む、現在のターン中に生成された項目です。
-   `input_items`: `new_items` の代わりに次のエージェントへ転送するオプションの項目です。セッション履歴用の `new_items` をそのまま維持しながら、モデル入力をフィルタリングできます。
-   `run_context`: ハンドオフが呼び出された時点で有効だった [`RunContextWrapper`][agents.run_context.RunContextWrapper] です。

ネストされたハンドオフ履歴は、オプトイン形式のベータ版として利用でき、安定化を進めている間はデフォルトで無効になっています。[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を有効にすると、Runner は、要約可能な履歴を順序付けされたアシスタント要約セグメントに圧縮しながら、情報を損なわないメッセージ項目を元の位置に保持します。生成される各要約セグメントでは `<CONVERSATION HISTORY>` ラッパーが使用され、その後のハンドオフでは、順序付けされたトランスクリプトを再構築する前に、以前に生成されたセグメントがフラット化されます。セッション、`RunState`、`RunResult.to_input_list()` は、SDK のデフォルト履歴に移動されたメッセージの出現箇所を正確に追跡し、同じ出現箇所が二重に追加されないようにします。内容が同一でも別個のメッセージは保持されます。[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を介して独自のマッピング関数を指定し、組み込みのセグメント分割を使用する代わりに、次のエージェント向けの入力項目の正確なリストを返すこともできます。このオプトインは、ハンドオフの `input_filter` と有効な実行の `RunConfig.handoff_input_filter` のどちらも設定されていない場合にのみ適用されます。そのため、ペイロードをすでにカスタマイズしている既存のコード（このリポジトリ内のコード例を含む）は、変更なしで現在の動作を維持します。[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡すことで、単一のハンドオフに対するネスト動作をオーバーライドできます。これにより、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] が設定されます。生成される要約セグメントのラッパーテキストのみを変更する場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出してください。後続の実行でデフォルトのラッパーに戻す必要がある場合は、事前に [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] を呼び出してください。

ネストされたハンドオフ履歴は、トランスクリプトの表現方法を変更しますが、機密データを秘匿化するものではありません。対応する構造化ツール項目が個別に転送されなくなった場合でも、ツール呼び出し引数とツール出力が、生成されたアシスタント要約に残ることがあります。受信側のエージェントとそのモデルプロバイダーは、転送される履歴の受信者として扱ってください。

クライアント管理の履歴では、明示的な [`input_filter`][agents.handoffs.Handoff.input_filter] または [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] を使用して、受信側のエージェントが参照できる内容を選択または秘匿化してください。カスタムフィルターから `nest_handoff_history` も呼び出す場合は、その呼び出しの前に `input_history`、`pre_handoff_items`、`new_items` をサニタイズしてください。このヘルパーは、これら 3 つのフィールドからネストされた履歴を構築し、既存の `input_items` オーバーライドを無視します。そのため、`input_items` だけをフィルタリングすると、除外したツールの内容が生成された要約に残る可能性があります。

セッション履歴用に元の `new_items` を保持する必要がある場合、フィルターは代わりに `nest_handoff_history` を呼び出し、ネストされた実行結果を返す前に、返された `input_history` をサニタイズできます。ネスト後に `input_items` だけを消去または置換しても、すでに `input_history` に含まれている内容は削除されません。

サーバー管理の会話（`conversation_id`、`previous_response_id`、または `auto_previous_response_id`）では、ハンドオフ入力フィルターはサポートされません。受信側のエージェントにそのサーバー管理の履歴を継承させてはならない場合は、入力を明示的に選択した別の実行を使用してください。その別の実行では、元の `conversation_id` または `previous_response_id` を再利用しないでください。

ハンドオフと有効な [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] の両方でフィルターが定義されている場合、その特定のハンドオフでは、ハンドオフごとの [`input_filter`][agents.handoffs.Handoff.input_filter] が優先されます。

!!! note

    ハンドオフは、単一の実行内にとどまります。入力ガードレールは引き続きチェーン内の最初のエージェントにのみ適用され、出力ガードレールは最終出力を生成するエージェントにのみ適用されます。ワークフロー内の各カスタム関数ツール呼び出しの前後でチェックが必要な場合は、ツールガードレールを使用してください。

履歴からすべてのツール呼び出しを削除するなど、一般的なパターンの一部は、[`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools,  # (1)!
)
```

1. `FAQ agent` が呼び出されると、履歴からツールに関連するすべての項目が自動的に削除されます。

`remove_all_tools` は、構造化ツール項目を削除します。通常のメッセージまたはネストされた履歴の要約にすでにコピーされたツール引数や結果は秘匿化しません。必要に応じて、それらのメッセージ内容を削除または秘匿化するには、カスタム入力フィルターを使用してください。

## 推奨プロンプト {#recommended-prompts}

LLM がハンドオフを正しく理解できるように、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨プレフィックスが用意されています。また、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出すことで、推奨データをプロンプトへ自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```