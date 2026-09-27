---
search:
  exclude: true
---
# ハンドオフ

ハンドオフを使用すると、エージェントは別のエージェントにタスクを委任できます。これは、複数のエージェントがそれぞれ異なる領域を専門としている場合に特に便利です。たとえば、カスタマーサポートアプリでは、注文状況、返金、FAQ などのタスクを個別に処理するエージェントを用意できます。

ハンドオフは、LLM に対してツールとして表現されます。そのため、`Refund Agent` という名前のエージェントへのハンドオフがある場合、ツールの名前は `transfer_to_refund_agent` になります。

## ハンドオフの作成 {#creating-a-handoff}

すべてのエージェントには [`handoffs`][agents.agent.Agent.handoffs] パラメーターがあり、`Agent` を直接受け取ることも、ハンドオフをカスタマイズする `Handoff` オブジェクトを受け取ることもできます。

通常の `Agent` インスタンスを渡すと、その [`handoff_description`][agents.agent.Agent.handoff_description] が設定されている場合、デフォルトのツール説明に追加されます。完全な `handoff()` オブジェクトを記述せずに、モデルがそのハンドオフを選択すべきタイミングを示すために使用できます。

Agents SDK が提供する [`handoff()`][agents.handoffs.handoff] 関数を使用して、ハンドオフを作成できます。この関数では、ハンドオフ先のエージェントに加え、オプションのオーバーライドや入力フィルターを指定できます。

### 基本的な使用方法 {#basic-usage}

次のように、単純なハンドオフを作成できます。

```python
from agents import Agent, handoff

billing_agent = Agent(name="Billing agent")
refund_agent = Agent(name="Refund agent")

# (1)!
triage_agent = Agent(name="Triage agent", handoffs=[billing_agent, handoff(refund_agent)])
```

1. エージェントを直接使用することも（`billing_agent` のように）、`handoff()` 関数を使用することもできます。

### `handoff()` 関数によるハンドオフのカスタマイズ {#customizing-handoffs-via-the-handoff-function}

[`handoff()`][agents.handoffs.handoff] 関数を使用すると、さまざまな設定をカスタマイズできます。

-   `agent`: ハンドオフ先となるエージェントです。
-   `tool_name_override`: デフォルトでは `Handoff.default_tool_name()` 関数が使用され、`transfer_to_<agent_name>` に解決されます。これはオーバーライドできます。
-   `tool_description_override`: `Handoff.default_tool_description()` によるデフォルトのツール説明をオーバーライドします。
-   `on_handoff`: ハンドオフが呼び出されたときに実行されるコールバック関数です。ハンドオフが呼び出されることが判明した時点で、データ取得を開始する場合などに便利です。この関数はエージェントコンテキストを受け取り、オプションで LLM が生成した入力も受け取れます。入力データは `input_type` パラメーターで制御されます。
-   `input_type`: ハンドオフのツール呼び出し引数のスキーマです。設定すると、解析済みのペイロードが `on_handoff` に渡されます。
-   `input_filter`: 次のエージェントが受け取る入力をフィルタリングできます。詳しくは以下をご覧ください。
-   `is_enabled`: ハンドオフが有効かどうかを指定します。ブール値またはブール値を返す関数を指定でき、実行時にハンドオフを動的に有効化または無効化できます。
-   `nest_handoff_history`: RunConfig レベルの `nest_handoff_history` 設定に対する、ハンドオフごとのオプションのオーバーライドです。`None` の場合、アクティブな実行設定で定義された値が代わりに使用されます。

[`handoff()`][agents.handoffs.handoff] ヘルパーは、渡された特定の `agent` に常に制御を移します。複数の移行先が考えられる場合は、移行先ごとにハンドオフを 1 つ登録し、その中からモデルに選択させます。呼び出し時に返すエージェントを独自のハンドオフコードで決定する必要がある場合にのみ、カスタムの [`Handoff`][agents.handoffs.Handoff] を使用してください。

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

状況によっては、LLM がハンドオフを呼び出す際に、何らかのデータを提供するようにしたい場合があります。たとえば、「エスカレーションエージェント」へのハンドオフを考えてみましょう。ログに記録できるように、モデルから理由を提供させることができます。

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

`is_enabled` は、モデルがハンドオフ引数を返す前に、SDK が利用可能なハンドオフを準備する段階で評価されるため、引数を持つハンドオフ内の値を認可することはできません。認可が解析済みフィールドに依存する場合は、アプリケーションで副作用が発生する前に、`on_handoff` の先頭でチェックを実行してください。認可に失敗した場合は、値を返すのではなく例外を送出してください。`on_handoff` が正常に返ると、SDK は移行処理を続行します。ツール入力ガードレールは関数ツールに適用され、ハンドオフには適用されません。

これは、次のエージェントのメイン入力を置き換えるものではなく、別の移行先を選択するものでもありません。[`handoff()`][agents.handoffs.handoff] ヘルパーは引き続き、ラップした特定のエージェントに移行し、受信側のエージェントも、[`input_filter`][agents.handoffs.Handoff.input_filter] またはネストされたハンドオフ履歴の設定で変更しない限り、会話履歴を引き続き参照します。

`input_type` は [`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] とも別のものです。`input_type` は、すでにローカルに存在するアプリケーション状態や依存関係ではなく、ハンドオフ時にモデルが決定するメタデータに使用してください。

### `input_type` の使用場面 {#when-to-use-input_type}

ハンドオフで、`reason`、`language`、`priority`、`summary` など、モデルが生成する少量のメタデータが必要な場合は、`input_type` を使用します。たとえば、トリアージエージェントは `{ "reason": "duplicate_charge", "priority": "high" }` とともに返金エージェントへハンドオフでき、返金エージェントが引き継ぐ前に、`on_handoff` でそのメタデータをログに記録または永続化できます。

目的が異なる場合は、別の仕組みを選択してください。

-   既存のアプリケーション状態と依存関係は、[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] に格納します。[コンテキストガイド](context.md)をご覧ください。
-   受信側のエージェントに表示される履歴を変更する場合は、[`input_filter`][agents.handoffs.Handoff.input_filter]、[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history]、または [`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を使用します。
-   複数の専門エージェントが移行先の候補となる場合は、移行先ごとにハンドオフを 1 つ登録します。`input_type` は選択されたハンドオフにメタデータを追加できますが、移行先を振り分けるものではありません。
-   会話を移行せず、ネストされた専門エージェントに構造化入力を渡す場合は、[`Agent.as_tool(parameters=...)`][agents.agent.Agent.as_tool] の使用を推奨します。[ツール](tools.md#structured-input-for-tool-agents)をご覧ください。

## 入力フィルター {#input-filters}

ハンドオフが発生すると、新しいエージェントが会話を引き継ぎ、それまでの会話履歴全体を参照できるようになります。これを変更する場合は、[`input_filter`][agents.handoffs.Handoff.input_filter] を設定できます。入力フィルターは、[`HandoffInputData`][agents.handoffs.HandoffInputData] を介して既存の入力を受け取り、新しい `HandoffInputData` を返す必要がある関数です。

[`HandoffInputData`][agents.handoffs.HandoffInputData] には、以下が含まれます。

-   `input_history`: `Runner.run(...)` が開始される前の入力履歴です。
-   `pre_handoff_items`: ハンドオフが呼び出されたエージェントターンより前に生成された項目です。
-   `new_items`: ハンドオフ呼び出しとハンドオフ出力項目を含む、現在のターン中に生成された項目です。
-   `input_items`: `new_items` の代わりに次のエージェントへ転送するオプションの項目です。セッション履歴用の `new_items` をそのまま維持しながら、モデル入力をフィルタリングできます。
-   `run_context`: ハンドオフが呼び出された時点でアクティブだった [`RunContextWrapper`][agents.run_context.RunContextWrapper] です。

ネストされたハンドオフ履歴は、オプトインのベータ機能として利用でき、安定化を進めている間はデフォルトで無効になっています。[`RunConfig.nest_handoff_history`][agents.run.RunConfig.nest_handoff_history] を有効にすると、ランナーは、要約可能な履歴を順序付けられたアシスタント要約セグメントに圧縮しつつ、情報を失わないメッセージ項目を元の位置に保持します。生成された各要約セグメントでは `<CONVERSATION HISTORY>` ラッパーが使用され、後続のハンドオフでは、順序付けられたトランスクリプトを再構築する前に、それ以前に生成されたセグメントがフラット化されます。セッション、`RunState`、および `RunResult.to_input_list()` は、この SDK デフォルト履歴に移されたメッセージの出現箇所を正確に追跡し、同じ出現箇所が二重に追加されないようにします。内容が同一でも別個のメッセージは引き続き保持されます。[`RunConfig.handoff_history_mapper`][agents.run.RunConfig.handoff_history_mapper] を介して独自のマッピング関数を指定し、組み込みのセグメント化を使用する代わりに、次のエージェントに渡す入力項目の正確なリストを返すこともできます。このオプトインは、ハンドオフの `input_filter` とアクティブな実行の `RunConfig.handoff_input_filter` のどちらも設定されていない場合にのみ適用されるため、すでにペイロードをカスタマイズしている既存のコード（このリポジトリのコード例を含む）は、変更なしで現在の動作を維持します。[`handoff(...)`][agents.handoffs.handoff] に `nest_handoff_history=True` または `False` を渡すことで、単一のハンドオフに対するネスト動作をオーバーライドできます。これにより、[`Handoff.nest_handoff_history`][agents.handoffs.Handoff.nest_handoff_history] が設定されます。生成される要約セグメントのラッパーテキストのみを変更する場合は、エージェントを実行する前に [`set_conversation_history_wrappers`][agents.handoffs.set_conversation_history_wrappers] を呼び出します。後の実行でデフォルトのラッパーに戻す必要がある場合は、その前に [`reset_conversation_history_wrappers`][agents.handoffs.reset_conversation_history_wrappers] を呼び出します。

ネストされたハンドオフ履歴ではトランスクリプトの表現方法が変わりますが、機密データは秘匿されません。対応する構造化ツール項目が個別に転送されなくなった場合でも、ツール呼び出しの引数とツール出力が、生成されたアシスタント要約に残ることがあります。受信側のエージェントとそのモデルプロバイダーは、転送される履歴の受信者として扱ってください。

クライアント管理の履歴では、明示的な [`input_filter`][agents.handoffs.Handoff.input_filter] または [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] を使用して、受信側のエージェントが参照できるコンテンツを選択または秘匿します。カスタムフィルターでも `nest_handoff_history` を呼び出す場合は、その呼び出しの前に `input_history`、`pre_handoff_items`、`new_items` をサニタイズしてください。このヘルパーは、これら 3 つのフィールドからネストされた履歴を構築し、既存の `input_items` オーバーライドを無視します。そのため、`input_items` だけをフィルタリングすると、除外したツールコンテンツが生成された要約に残る可能性があります。

フィルターでセッション履歴用の元の `new_items` を維持する必要がある場合は、代わりに `nest_handoff_history` を呼び出し、返された `input_history` をサニタイズしてから、ネストされた実行結果を返すことができます。ネスト後に `input_items` だけをクリアまたは置換しても、すでに `input_history` に含まれているコンテンツは削除されません。

サーバー管理の会話（`conversation_id`、`previous_response_id`、または `auto_previous_response_id`）では、ハンドオフ入力フィルターはサポートされません。受信側のエージェントにそのサーバー管理の履歴を継承させない場合は、明示的に選択した入力を使用して別の実行を行ってください。その別の実行では、元の `conversation_id` または `previous_response_id` を再利用しないでください。

ハンドオフとアクティブな [`RunConfig.handoff_input_filter`][agents.run.RunConfig.handoff_input_filter] の両方でフィルターが定義されている場合、その特定のハンドオフでは、ハンドオフごとの [`input_filter`][agents.handoffs.Handoff.input_filter] が優先されます。

!!! note

    ハンドオフは単一の実行内に留まります。入力ガードレールは引き続きチェーン内の最初のエージェントにのみ適用され、出力ガードレールは最終出力を生成するエージェントにのみ適用されます。ワークフロー内の各カスタム関数ツール呼び出しの前後でチェックが必要な場合は、ツールガードレールを使用してください。

一般的なパターン（履歴からすべてのツール呼び出しを削除するなど）は、[`agents.extensions.handoff_filters`][] に実装されています。

```python
from agents import Agent, handoff
from agents.extensions import handoff_filters

agent = Agent(name="FAQ agent")

handoff_obj = handoff(
    agent=agent,
    input_filter=handoff_filters.remove_all_tools, # (1)!
)
```

1. `FAQ agent` が呼び出されると、履歴からすべてのツール関連項目が自動的に削除されます。

`remove_all_tools` は構造化されたツール項目を削除します。通常のメッセージまたはネストされた履歴の要約にすでにコピーされたツール引数や実行結果は秘匿されません。必要に応じて、カスタム入力フィルターを使用して、それらのメッセージ内容を削除または秘匿してください。

## 推奨プロンプト {#recommended-prompts}

LLM がハンドオフを正しく理解できるようにするため、エージェントにハンドオフに関する情報を含めることを推奨します。[`agents.extensions.handoff_prompt.RECOMMENDED_PROMPT_PREFIX`][] に推奨プレフィックスが用意されています。また、[`agents.extensions.handoff_prompt.prompt_with_handoff_instructions`][] を呼び出すことで、推奨データをプロンプトに自動的に追加できます。

```python
from agents import Agent
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX

billing_agent = Agent(
    name="Billing agent",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    <Fill in the rest of your prompt here>.""",
)
```