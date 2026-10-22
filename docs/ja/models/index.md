---
search:
  exclude: true
---
# モデル

Agents SDK は、すぐに利用できる OpenAI モデルのサポートを次の 2 つの形式で提供します。

- **推奨**: 新しい [Responses API](https://platform.openai.com/docs/api-reference/responses) を使用して OpenAI API を呼び出す [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]。
- [Chat Completions API](https://platform.openai.com/docs/api-reference/chat) を使用して OpenAI API を呼び出す [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]。

## Decisions API {#decisions-api}

エージェント実行と並行して分類を行うには、[OpenAI Decisions API](https://developers.openai.com/api/docs/guides/decisions) を使用します。`AsyncOpenAI.decisions.create()` を直接呼び出し、その回答を既存の Agents SDK ガードレール、承認コールバック、またはモデル選択ステップで使用します。Decisions は Agents SDK の `Model` インターフェースを実装していません。

ネイティブの Decisions クライアントを使用するには、`openai>=3.26.0,<4` をインストールします。Agents SDK の最小依存バージョンだけでは、Decisions リソースが利用可能であることは保証されません。カスタム認証情報やベース URL を含め、ネイティブクライアントを明示的に設定してください。`set_default_openai_client()` などの Agents SDK のデフォルト設定は、別途作成された `AsyncOpenAI` インスタンスには適用されません。

[実行可能な Decisions のコード例](https://github.com/openai/openai-agents-python/tree/main/examples/decisions)では、次の 3 つのワークフローを示しています。

| ワークフロー | 既存の SDK API | アプリケーション側の責任 |
| --- | --- | --- |
| 初期リクエストの確認 | `@input_guardrail(run_in_parallel=False)` | 述語としきい値を選択し、エージェントが開始する前に拒否を処理します。 |
| 関数ツールの内容のレビュー | `@tool_input_guardrail` または呼び出し可能な `needs_approval` | コンテンツポリシーを定義し、呼び出しを拒否するか、人間による承認を要求するかを選択します。 |
| 実行前のモデル選択 | `agent.clone(model=...)` | 選択肢の回答を許可されたモデルにマッピングし、拒否時のフォールバックを選択します。 |

### 名前による回答の読み取り {#read-answers-by-name}

Decisions リクエストには、述語、選択肢、スコアに関する質問を含められます。ネイティブレスポンスには `answers` リストが含まれます。質問名で回答をインデックス化し、型固有のフィールドを読み取る前に、各回答の `type` を確認します。

```python
from openai import AsyncOpenAI


async def is_support_request(client: AsyncOpenAI, text: str) -> bool:
    decision = await client.decisions.create(
        model="gpt-6-luna",
        input=text,
        questions=[
            {
                "type": "predicate",
                "name": "in_scope",
                "instructions": "Is this a request for help with a software product?",
            }
        ],
    )
    answers = {answer.name: answer for answer in decision.answers}
    answer = answers.get("in_scope")
    if answer is None or answer.type != "predicate":
        return False
    return answer.probability >= 0.8
```

上記のしきい値は例示です。アプリケーションのラベル付きサンプルを使用してしきい値を選択してください。拒否は 1 つの質問に適用されます。同じレスポンス内の別の質問は、引き続き回答を返す可能性があります。拒否、回答の欠落、API エラーを各ワークフローでどのように処理するかを個別に決定してください。コード例では、利用できない分類器を肯定的な回答として扱わず、API エラーをそのまま伝播させます。

### 統合境界の定義 {#define-the-integration-boundary}

入力ガードレールのコード例は単一のテキストリクエストを受け取り、チェックが完了するまでエージェントの実行をブロックします。ツールのコード例は告知の引数を評価し、公開はシミュレーションのみ行います。この分類器は編集レビューポリシーを実装しますが、ユーザーの認可は判定しません。権限は、信頼できるアプリケーションコードで別途適用してください。

呼び出し可能な `needs_approval` では、引数の検証に変換、デフォルト値、またはアプリケーション定義の検証が必要な場合、デコレーター付き関数ツールには手動承認が必要です。このような場合、SDK は条件付き承認コールバックを呼び出しません。明示的な承認後、SDK はそのコールバックを再評価せずに呼び出しを再開します。ツール入力ガードレールは独立したチェックであり、呼び出しの直前に再度実行できます。[human-in-the-loop](../human_in_the_loop.md) および[ツールガードレール](../guardrails.md#tool-guardrails)を参照してください。

実行前ルーティングでは、開始エージェントのモデルを選択します。ハンドオフ先のモデルを自動的に選択したり、再開した実行を再ルーティングしたりするものではありません。使用する分類根拠は自身で提供してください。これらのテキスト専用レシピは、サーバーに保持された履歴を取得したり、任意の会話項目や添付ファイルを Decisions の入力へ変換したりしません。

Decisions の直接呼び出しは、Runner のモデルアダプターの対象外です。その使用量はネイティブレスポンスで確認できますが、`RunResult.context_wrapper.usage` や Agents SDK の生成スパンには自動的に含まれません。コード例のように、`async with AsyncOpenAI() as client` を使用してネイティブクライアントのライフタイムを管理してください。

## モデル設定の選択 {#choosing-a-model-setup}

設定に適した最も単純な方法から始めてください。

| 目的 | 推奨される方法 | 詳細 |
| --- | --- | --- |
| OpenAI モデルのみの使用 | デフォルトの OpenAI プロバイダーを Responses モデル経路で使用 | [OpenAI モデル](#openai-models) |
| WebSocket 転送による OpenAI Responses API の使用 | Responses モデル経路を維持し、WebSocket 転送を有効化 | [Responses WebSocket 転送](#responses-websocket-transport) |
| OpenAI がホストするサブエージェントの使用 | 実験的なホスト型マルチエージェントモデルを使用 | [ホスト型マルチエージェント](#hosted-multi-agent-experimental) |
| 単一の非 OpenAI プロバイダーの使用 | 組み込みのプロバイダー統合ポイントから開始 | [非 OpenAI モデル](#non-openai-models) |
| エージェント間でのモデルまたはプロバイダーの混在 | 実行単位またはエージェント単位でプロバイダーを選択し、機能差を確認 | [単一ワークフロー内でのモデルの混在](#mixing-models-in-one-workflow)および[プロバイダー間でのモデルの混在](#mixing-models-across-providers) |
| OpenAI Responses の高度なリクエスト設定の調整 | OpenAI Responses 経路で `ModelSettings` を使用 | [OpenAI Responses の高度な設定](#advanced-openai-responses-settings) |
| 非 OpenAI または混合プロバイダールーティング向けのサードパーティ製アダプターの使用 | サポートされているベータ版アダプターを比較し、リリース予定のプロバイダー経路を検証 | [サードパーティ製アダプター](#third-party-adapters) |

## OpenAI モデル {#openai-models}

OpenAI のみを使用するほとんどのアプリでは、デフォルトの OpenAI プロバイダーで文字列のモデル名を使用し、Responses モデル経路を維持する方法を推奨します。

[`Agent`][agents.agent.Agent] でモデルを指定しない場合、Agents SDK はコスト重視で大量処理を行うエージェントワークフロー向けに、デフォルトで [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna) を `reasoning.effort="none"` および `verbosity="low"` とともに使用します。最先端の能力が必要なアプリケーションでは、`model="gpt-5.6-sol"` を明示的に設定し、ワークロードに適した `model_settings` を選択できます。

`gpt-5.6-sol` などの別のモデルへ切り替える場合、エージェントを設定する方法は 2 つあります。

### デフォルトモデル {#default-model}

まず、カスタムモデルを設定していないすべてのエージェントで特定のモデルを一貫して使用する場合は、エージェントを実行する前に `OPENAI_DEFAULT_MODEL` 環境変数を設定します。

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.6-sol
python3 my_awesome_agent.py
```

次に、`RunConfig` を使用して、実行単位のデフォルトモデルを設定できます。エージェントにモデルを設定していない場合、この実行のモデルが使用されます。

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.6-sol"),
)
```

#### GPT-5 モデル {#gpt-5-models}

この方法で `gpt-5.6-sol` などの GPT-5 モデルを使用すると、SDK はデフォルトの `ModelSettings` を適用します。これには、ほとんどのユースケースで最適に機能する設定が含まれます。デフォルトモデルの推論労力を調整するには、独自の `ModelSettings` を渡します。

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.6-sol is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.6-sol",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low"),
)
```

レイテンシーを抑えるには、GPT-5 モデルで `reasoning.effort="none"` を使用することを推奨します。

GPT-5.6 は、既存の `reasoning` 設定を通じて、推論モード、会話ターン間で引き継がれる推論コンテキスト、`"max"` 労力レベルもサポートします。これらの制御は Responses API 経路で利用できます。

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

agent = Agent(
    name="Deep research agent",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        reasoning=Reasoning(
            mode="pro",
            effort="max",
            context="all_turns",
        ),
    ),
)
```

`reasoning.mode` と `reasoning.context` は Responses 専用の設定です。Chat Completions では `reasoning.effort` のみが使用され、サポートされる労力レベルはモデルと API サーフェスによって異なります。GPT-5.6 の `"max"` 労力には Responses API を使用してください。Chat Completions アダプターは警告を出してモードとコンテキストを無視します。その警告をエラーに変更するには、OpenAI プロバイダーで `strict_feature_validation=True` を設定します。

`context="all_turns"` を使用する場合は、`previous_response_id`、サーバー側の Responses API 会話、または次のリクエストへの過去の推論項目の追加によって、会話を維持してください。ステートレスな `store=False` 呼び出しでは、レスポンスで `reasoning.encrypted_content` を要求し、それらの推論項目を次のリクエストの入力に含めます。

#### ComputerTool のモデル選択 {#computertool-model-selection}

エージェントに [`ComputerTool`][agents.tool.ComputerTool] が含まれる場合、実際の Responses リクエストで有効なモデルにより、SDK が送信するコンピューターツールのペイロードが決まります。エージェントに `model` が設定されていない場合、通常の SDK モデル選択優先順位が適用されます。現在 [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna) である SDK 組み込みのデフォルトは、GA 版の組み込み `computer` ツールをサポートします。`OPENAI_DEFAULT_MODEL` または `RunConfig.model` でそのデフォルトを上書きする場合は、コンピュータ操作をサポートするモデルを選択してください。コンピュータ操作ワークロードに別の機能とコストの特性を選択する場合は、エージェントに `model` を設定します。たとえば、`model="gpt-5.6"` は OpenAI が GPT-5.6 Sol にルーティングするエイリアスを使用します。明示的な `computer-use-preview` リクエストでは、以前の `computer_use_preview` ペイロードが維持されます。

主な例外は、プロンプト管理された呼び出しです。プロンプトテンプレートでモデルが指定され、SDK がリクエストから `model` を省略する場合、プロンプトが固定しているモデルを推測しないように、SDK はプレビュー互換のコンピューターペイロードをデフォルトで使用します。このフローで GA 経路を維持するには、`model="gpt-5.6"` などのサポート対象 GA モデルをリクエストで明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制します。

[`ComputerTool`][agents.tool.ComputerTool] が登録されている場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` は、有効なリクエストモデルに一致する組み込みセレクターへ正規化されます。`ComputerTool` が登録されていない場合、これらの文字列は通常の関数名として引き続き動作します。

プレビュー互換リクエストでは、`environment` と表示サイズを事前にシリアライズする必要があります。そのため、[`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを使用するプロンプト管理フローでは、具体的な `Computer` または `AsyncComputer` インスタンスを渡すか、リクエスト送信前に GA セレクターを強制してください。移行の詳細については、[ツール](../tools.md#computertool-and-the-responses-computer-tool)を参照してください。

#### GPT-5 以外のモデル {#non-gpt-5-models}

カスタムの `model_settings` を指定せずに GPT-5 以外のモデル名を渡すと、SDK はすべてのモデルと互換性のある汎用の `ModelSettings` に戻します。

### Responses 専用のツール機能 {#responses-only-tool-features}

次のツール機能は、OpenAI Responses モデルでのみサポートされています。

- [`ToolSearchTool`][agents.tool.ToolSearchTool]
- [`tool_namespace()`][agents.tool.tool_namespace]
- `@function_tool(defer_loading=True)` およびその他の遅延読み込み対応 Responses ツールサーフェス
- [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool]、`allowed_callers`、`tool_choice="programmatic_tool_calling"`

これらの機能は、Chat Completions モデルおよび Responses 以外のバックエンドでは拒否されます。遅延読み込みツールを使用する場合は、エージェントに `ToolSearchTool()` を追加し、モデルが `auto` または `required` のツール選択を通じてツールを読み込めるようにしてください。単独の名前空間名や遅延読み込み専用の関数名を強制しないでください。設定の詳細と現在の制約については、[ホスト型ツール検索](../tools.md#hosted-tool-search)および[プログラムによるツール呼び出し](../tools.md#programmatic-tool-calling)を参照してください。

### Responses WebSocket 転送 {#responses-websocket-transport}

デフォルトでは、OpenAI Responses API リクエストは HTTP 転送を使用します。OpenAI Responses プロバイダー経路を使用している場合は、WebSocket 転送を有効にできます。

#### 基本設定 {#basic-setup}

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

これは、デフォルトの OpenAI プロバイダーがモデル名を解決した結果として得られる OpenAI Responses モデルに影響します。これには `"gpt-5.6-sol"` などの文字列のモデル名も含まれます。

転送方式の選択は、SDK がモデル名をモデルインスタンスへ解決するときに行われます。具体的な [`Model`][agents.models.interface.Model] オブジェクトを渡す場合、その転送方式はすでに固定されています。[`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] は WebSocket、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] は HTTP を使用し、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は Chat Completions を維持します。`RunConfig(model_provider=...)` を渡す場合、グローバルなデフォルトではなく、そのプロバイダーが転送方式の選択を制御します。

#### プロバイダー単位または実行単位の設定 {#provider-or-run-level-setup}

プロバイダー単位または実行単位で WebSocket 転送を設定することもできます。

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses_websocket=True,
    # Optional; if omitted, OPENAI_WEBSOCKET_BASE_URL is used when set.
    websocket_base_url="wss://your-proxy.example/v1",
    # Optional low-level websocket keepalive settings.
    responses_websocket_options={"ping_interval": 20.0, "ping_timeout": 60.0},
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

SDK の OpenAI 統合を介してルーティングするプロバイダーは、オプションのエージェント登録設定も受け入れます。これは、OpenAI の設定でハーネス ID などのプロバイダー単位の登録メタデータが必要な場合に使用する高度なオプションです。

```python
from agents import (
    Agent,
    OpenAIAgentRegistrationConfig,
    OpenAIProvider,
    RunConfig,
    Runner,
)

provider = OpenAIProvider(
    use_responses_websocket=True,
    agent_registration=OpenAIAgentRegistrationConfig(harness_id="your-harness-id"),
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

#### `MultiProvider` による高度なルーティング {#advanced-routing-with-multiprovider}

プレフィックスに基づくモデルルーティングが必要な場合、たとえば 1 回の実行で `openai/...` と `any-llm/...` のモデル名を混在させる場合は、[`MultiProvider`][agents.MultiProvider] を使用し、そこで `openai_use_responses_websocket=True` を設定します。

`MultiProvider` には、従来からの次の 2 つのデフォルト動作があります。

- `openai/...` は OpenAI プロバイダーのエイリアスとして扱われるため、`openai/gpt-4.1` はモデル `gpt-4.1` としてルーティングされます。
- 不明なプレフィックスはそのまま渡されず、`UserError` が発生します。

リテラルの名前空間付きモデル ID を要求する OpenAI 互換エンドポイントを OpenAI プロバイダーで使用する場合は、パススルー動作を明示的に有効にしてください。WebSocket が有効な設定では、`MultiProvider` にも `openai_use_responses_websocket=True` を維持してください。

```python
from agents import Agent, MultiProvider, RunConfig, Runner

provider = MultiProvider(
    openai_base_url="https://openrouter.ai/api/v1",
    openai_api_key="...",
    openai_use_responses_websocket=True,
    openai_prefix_mode="model_id",
    unknown_prefix_mode="model_id",
)

agent = Agent(
    name="Assistant",
    instructions="Be concise.",
    model="openai/gpt-4.1",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

バックエンドがリテラルの `openai/...` 文字列を要求する場合は、`openai_prefix_mode="model_id"` を使用します。バックエンドが `openrouter/openai/gpt-4.1-mini` など、その他の名前空間付きモデル ID を要求する場合は、`unknown_prefix_mode="model_id"` を使用します。これらのオプションは、WebSocket 転送以外の `MultiProvider` でも機能します。このコード例では、このセクションで説明している転送設定の一部であるため、WebSocket を有効なままにしています。同じオプションは [`responses_websocket_session()`][agents.responses_websocket_session] でも利用できます。

`MultiProvider` を介してルーティングしながら、同じプロバイダー単位の登録メタデータが必要な場合は、`openai_agent_registration=OpenAIAgentRegistrationConfig(...)` を渡します。これは基盤となる OpenAI プロバイダーへ転送されます。

カスタムの OpenAI 互換エンドポイントまたはプロキシを使用する場合、WebSocket 転送には互換性のある WebSocket `/responses` エンドポイントも必要です。このような設定では、`websocket_base_url` を明示的に設定する必要がある場合があります。

#### 注意事項 {#notes}

- これは WebSocket 転送経由の Responses API であり、[Realtime API](../realtime/guide.md) ではありません。Chat Completions には適用されません。非 OpenAI プロバイダーには、Responses WebSocket の `/responses` エンドポイントをサポートしている場合にのみ適用されます。
- 環境でまだ利用できない場合は、`websockets` パッケージをインストールしてください。
- WebSocket 転送を有効にした後、[`Runner.run_streamed()`][agents.run.Runner.run_streamed] を直接使用できます。複数ターンのワークフローで、ターン間およびネストされた Agents-as-tools 呼び出し間でも同じ WebSocket 接続を再利用する場合は、[`responses_websocket_session()`][agents.responses_websocket_session] ヘルパーを推奨します。[エージェントの実行](../running_agents.md)ガイドおよび [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py) を参照してください。
- 長時間の推論ターンやレイテンシーが急増するネットワークでは、`responses_websocket_options` を使用して WebSocket のキープアライブ動作をカスタマイズします。遅延した pong フレームを許容するには `ping_timeout` を増やします。ping を有効に保ちながらハートビートのタイムアウトを無効にするには、`ping_timeout=None` を設定します。WebSocket のレイテンシーより信頼性が重要な場合は、HTTP/SSE 転送を使用してください。
- デフォルトでは、SDK は受信メッセージのサイズ制限を無効にします（`max_size=None`）。プロキシの背後で長時間稼働するエージェントプロセスや、メモリが制限されたコンテナでは、メッセージ単位のメモリ使用量を制限するために `responses_websocket_options={"max_size": 8 * 1024 * 1024}` を設定します。
- [Responses API WebSocket サービス](https://developers.openai.com/api/docs/guides/websocket-mode)は、各接続で一度に 1 つのレスポンスを処理し、各接続を 60 分に制限します。この制限後は新しい接続を開いてください。並列実行が必要な場合は、複数の接続を使用します。
- サービスは、接続ローカルのメモリに最新のレスポンスのみを保持します。失敗した `4xx` または `5xx` のターンでは、`previous_response_id` が参照するレスポンスがそのメモリから削除されます。再接続後も、保存済みのレスポンスが利用可能であれば続行できますが、`store=False` および ZDR フローには永続化されたフォールバックがありません。`previous_response_id=None` で新しいチェーンを開始して完全な入力コンテキストを送信するか、ローカルで管理しているセッション状態からそのコンテキストを再構築してください。

### ホスト型マルチエージェント（実験的） {#hosted-multi-agent-experimental}

OpenAI Responses API のホスト型マルチエージェントベータでは、GPT-5.6 のルートモデルがサーバーでホストされるサブエージェントを作成し、調整できます。Agents SDK は通常の `Runner` を引き続き使用できます。ホスト型オーケストレーションはサービス側で維持され、開発者が定義した関数ツールはアプリケーション内で実行されます。

この統合は実験的であり、ローカル関数の出力を `response.inject` によってアクティブなホスト型エージェントへ返せるよう、Responses WebSocket 転送を使用します。`client.beta.responses.connect` を公開するバージョン 2.45.0 以降の `openai[realtime]` ビルドが必要です。インターフェースとベータ版の項目スキーマは、一般提供前に変更される可能性があります。

#### モデルの設定 {#configure-the-model}

実験的モジュールからモデルをインポートし、SDK の `Agent` に割り当てます。

```python
from agents import Agent
from agents.extensions.experimental.hosted_multi_agent import OpenAIHostedMultiAgentModel

agent = Agent(
    name="Research coordinator",
    instructions="Delegate independent research tasks, then synthesize the findings.",
    model=OpenAIHostedMultiAgentModel(model="gpt-5.6-sol", config={"max_concurrent_subagents": 3}),
)
```

`OpenAIHostedMultiAgentModel` を作成すると、`multi_agent.enabled` が有効になり、`OpenAI-Beta: responses_multi_agent=v1` WebSocket ヘッダーが送信されます。`openai_client` を指定しない限り、このモデルはデフォルトの OpenAI クライアントを使用します。`max_concurrent_subagents` を省略すると、サービスのデフォルトが使用されます。

#### ローカル関数ツール {#local-function-tools}

すべてのホスト型エージェントは、リクエストに設定されたモデルとツールを共有します。どのホスト型エージェントが関数を呼び出すかは Responses API が決定します。通常の SDK Runner が関数をローカルで実行し、同じ呼び出し ID を持つ `function_call_output` をアクティブな WebSocket レスポンスへ挿入します。これにより、サービスは元のホスト型呼び出し元を再開できます。関数の実行には、Runner の通常のガードレール、フック、失敗変換が引き続き適用されます。SDK のツール承認による中断はサポートされません。`needs_approval` 設定が `False` ではない関数ツールは、リクエスト送信前に拒否されます。

ツールで呼び出し元を考慮したログ記録または認可が必要な場合は、`get_hosted_agent_metadata()` を使用します。

```python
from typing import Any

from agents.decorators import tool
from agents.extensions.experimental.hosted_multi_agent import get_hosted_agent_metadata
from agents.tool_context import ToolContext


@tool
def lookup_document(ctx: ToolContext[Any], section: str) -> str:
    metadata = get_hosted_agent_metadata(ctx)
    caller = metadata.agent_name if metadata else "unknown"
    print(f"tool caller: {caller}; call ID: {ctx.tool_call_id}")
    return f"Contents for {section}"
```

ホスト型エージェント名は観測用のメタデータであり、ローカルルーティングの仕組みではありません。SDK が提供する呼び出し ID を使用して出力をルーティングしてください。副作用を伴うツールでは、その呼び出し ID を冪等性キーとして使用し、ツール実行前または実行中に必要な認可をアプリケーションコードで適用してください。このモデルでは `needs_approval` を使用しないでください。ツールの引数と出力は Responses API の境界を越えます。

#### 出力とストリーミングの動作 {#output-and-streaming-behavior}

フェーズが `final_answer` で、`/root` に帰属するメッセージのみが、通常の最終メッセージになります。実験的アダプターは、サブエージェントのメッセージとホスト型オーケストレーションのレコードを上位レベルの `RunResult` から除外します。SDK がこれらのレコードをローカル関数として実行することはありません。

raw ストリーミングでは、ホスト型出力項目や `response.inject.created` の確認応答を含む、ベータ版の Responses イベントが引き続き公開されます。アダプターは、関数呼び出しの準備ができると、アクティブな 1 つのプロバイダーレスポンスを SDK から見える論理的なモデルターンに分割し、Runner が出力を生成した後に同じプロバイダーレスポンスを再開します。raw のホスト型項目または `ToolContext` とともに `get_hosted_agent_metadata()` を使用すると、その項目またはツール呼び出しがどのホスト型エージェントに帰属するかを特定できます。

#### SDK オーケストレーションとの関係 {#relationship-to-sdk-orchestration}

ホスト型マルチエージェントは、SDK のハンドオフおよび Agents-as-tools とは別の機能です。

- ホスト型マルチエージェントは、OpenAI サービス上でサブエージェントを作成します。アプリケーションがそれらのサブエージェントを作成またはスケジュールすることはありません。
- SDK のハンドオフは、アクティブなローカル SDK の `Agent` を変更します。この実験的モデルを使用する場合、すべてのホスト型エージェントが同じハンドオフツールを受け取り、所有権の競合が発生するため、ハンドオフは拒否されます。
- Agents-as-tools は引き続き利用できますが、使用するとクライアント側とサーバー側のオーケストレーションがネストされます。追加のレイテンシー、コスト、ツールの公開範囲を慎重に評価してください。

#### 現在の制限事項 {#current-limitations}

実験的モデルは、`reasoning.summary`、`max_tool_calls`、および呼び出し元が指定する `multi_agent` または `betas` の上書きを拒否します。Responses の `/compact` エンドポイントはベータ版でサポートされていません。ただし、サービスが各ホスト型エージェントのコンテキストを個別に自動圧縮するため、明示的な `context_management.compact_threshold` は使用できます。

1 つの `OpenAIHostedMultiAgentModel` インスタンスが同時に所有できるアクティブなホスト型レスポンスは、最大 1 つです。ローカル関数の出力を待機中に実行を破棄する場合は、`await model.close()` を呼び出して WebSocket を解放してください。進行中のホスト型レスポンスを別のプロセスまたはイベントループで復元することは、現在サポートされていません。

基盤となる Responses API のベータ版の動作については、[OpenAI マルチエージェントガイド](https://developers.openai.com/api/docs/guides/tools-multi-agent)を参照してください。非ストリーミングおよびストリーミングでの SDK の使用方法については、[`examples/agent_patterns/hosted_multi_agent_beta.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/hosted_multi_agent_beta.py) を参照してください。

## 非 OpenAI モデル {#non-openai-models}

非 OpenAI プロバイダーが必要な場合は、SDK の組み込みプロバイダー統合ポイントから始めてください。多くの設定では、サードパーティ製アダプターを追加しなくても、これで十分です。各パターンのコード例は、[examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/) にあります。

### 非 OpenAI プロバイダーの統合方法 {#ways-to-integrate-non-openai-providers}

| 方法 | 使用する状況 | 適用範囲 |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | 1 つの OpenAI 互換エンドポイントを、ほとんどまたはすべてのエージェントのデフォルトにする場合 | グローバルデフォルト |
| [`ModelProvider`][agents.models.interface.ModelProvider] | 1 つのカスタムプロバイダーを単一の実行に適用する場合 | 実行単位 |
| [`Agent.model`][agents.agent.Agent.model] | エージェントごとに異なるプロバイダーまたは具体的なモデルオブジェクトが必要な場合 | エージェント単位 |
| サードパーティ製アダプター | 組み込み経路では提供されないプロバイダー対応またはルーティングをアダプターから利用する必要がある場合 | [サードパーティ製アダプター](#third-party-adapters)を参照 |

次の組み込み経路を使用して、他の LLM プロバイダーを統合できます。

1. [`set_default_openai_client`][agents.set_default_openai_client] は、`AsyncOpenAI` のインスタンスを LLM クライアントとしてグローバルに使用する場合に便利です。これは、LLM プロバイダーに OpenAI 互換 API エンドポイントがあり、`base_url` と `api_key` を設定できる場合に使用します。設定可能なコード例については、[examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py) を参照してください。
2. [`ModelProvider`][agents.models.interface.ModelProvider] は `Runner.run` レベルで機能します。これにより、「この実行のすべてのエージェントでカスタムモデルプロバイダーを使用する」と指定できます。設定可能なコード例については、[examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py) を参照してください。
3. [`Agent.model`][agents.agent.Agent.model] を使用すると、特定の Agent インスタンスにモデルを指定できます。これにより、エージェントごとに異なるプロバイダーを組み合わせられます。設定可能なコード例については、[examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py) を参照してください。

`platform.openai.com` の API キーがない場合は、`set_tracing_disabled()` でトレーシングを無効にするか、[別のトレーシングプロセッサー](../tracing.md)を設定することを推奨します。

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent = Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    これらのコード例では、多くの LLM プロバイダーがまだ Responses API をサポートしていないため、Chat Completions API／モデルを使用しています。利用する LLM プロバイダーが Responses API をサポートしている場合は、Responses の使用を推奨します。

## 単一ワークフロー内でのモデルの混在 {#mixing-models-in-one-workflow}

単一のワークフロー内で、エージェントごとに異なるモデルを使用したい場合があります。たとえば、トリアージには小さく高速なモデルを使用し、複雑なタスクには大きく高性能なモデルを使用できます。[`Agent`][agents.Agent] を設定するときは、次のいずれかの方法で特定のモデルを選択できます。

1. モデル名を渡します。
2. 任意のモデル名と、その名前を Model インスタンスへマッピングできる [`ModelProvider`][agents.models.interface.ModelProvider] を渡します。
3. [`Model`][agents.models.interface.Model] の実装を直接指定します。

!!! note

    SDK は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] と [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] の両方の形式をサポートしていますが、この 2 つの形式でサポートする機能とツールのセットが異なるため、ワークフローごとに 1 つのモデル形式を使用することを推奨します。ワークフローで複数のモデル形式を組み合わせる必要がある場合は、使用するすべての機能が両方で利用可能であることを確認してください。

```python
import asyncio

from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
    model="gpt-5-mini",  # (1)!
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model=OpenAIChatCompletionsModel(  # (2)!
        model="gpt-5-nano", openai_client=AsyncOpenAI()
    ),
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
    model="gpt-5.6-sol",
)


async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

1.  OpenAI モデルの名前を直接設定します。
2.  [`Model`][agents.models.interface.Model] の実装を指定します。

エージェントで使用するモデルをさらに設定する場合は、temperature などのオプションのモデル設定パラメーターを提供する [`ModelSettings`][agents.model_settings.ModelSettings] を渡せます。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

## OpenAI Responses の高度な設定 {#advanced-openai-responses-settings}

OpenAI Responses 経路を使用していて、より詳細な制御が必要な場合は、`ModelSettings` から始めてください。

### 一般的な高度な `ModelSettings` オプション {#common-advanced-modelsettings-options}

OpenAI Responses API を使用している場合、複数のリクエストフィールドには対応する `ModelSettings` フィールドがすでに直接用意されているため、それらに `extra_args` を使用する必要はありません。

- `parallel_tool_calls`: 同じターンで複数のツール呼び出しを許可または禁止します。
- `truncation`: コンテキストが上限を超える場合に失敗させる代わりに、Responses API が最も古い会話項目を削除できるよう、`"auto"` を設定します。
- `store`: 後で取得できるよう、生成されたレスポンスをサーバー側に保存するかどうかを制御します。これは、レスポンス ID に依存するフォローアップワークフローや、`store=False` の場合にローカル入力へフォールバックする必要があるセッション圧縮フローで重要です。
- `context_management`: `compact_threshold` を使用した Responses の圧縮など、サーバー側のコンテキスト処理を設定します。
- `prompt_cache_retention`: 以前のモデルファミリー向けの保持期間延長を設定します。たとえば、`"24h"` を使用します。
- `prompt_cache_options`: 暗黙的または明示的なプロンプトキャッシュを選択し、GPT-5.6 では `"30m"` のキャッシュ TTL を設定します。
- `response_include`: `web_search_call.action.sources`、`file_search_call.results`、`reasoning.encrypted_content` など、より詳細なレスポンスペイロードを要求します。
- `top_logprobs`: 出力テキストについて、確率上位トークンの logprobs を要求します。SDK は `message.output_text.logprobs` も自動的に追加します。
- `retry`: モデル呼び出しに対して Runner が管理する再試行設定を有効にします。[Runner が管理する再試行](#runner-managed-retries)を参照してください。

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        context_management=[{"type": "compaction", "compact_threshold": 200000}],
        prompt_cache_options={"mode": "explicit", "ttl": "30m"},
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

明示的なプロンプトキャッシュでは、再利用可能なプレフィックスの末尾となるコンテンツ部分にブレークポイントを追加します。同じ `ModelSettings.prompt_cache_options` フィールドが Responses と Chat Completions のリクエストにそのまま渡され、Chat Completions コンバーターはテキスト、画像、音声、ファイルのコンテンツ部分にあるブレークポイントを維持します。

```python
from agents import Runner

result = await Runner.run(
    research_agent,
    [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Reusable background material...",
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                },
                {
                    "type": "input_text",
                    "text": "Analyze the latest question.",
                },
            ],
        }
    ],
)
```

`prompt_cache_retention` は、従来の保持制御を使用する以前のモデルファミリーで引き続き利用できます。直接指定する `ModelSettings` フィールドと、`extra_args` 内の同じキーを組み合わせないでください。

`store=False` を設定すると、Responses API はそのレスポンスを後でサーバー側から取得できる状態で保持しません。これはステートレスまたはゼロデータ保持形式のフローに便利ですが、通常はレスポンス ID を再利用する機能が、代わりにローカルで管理する状態へ依存する必要があることも意味します。たとえば、最後のレスポンスが保存されていない場合、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] はデフォルトの `"auto"` 圧縮経路を入力ベースの圧縮に切り替えます。[セッションガイド](../sessions/index.md#openai-responses-compaction-sessions)を参照してください。

サーバー側の圧縮は、[`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] とは異なります。`context_management=[{"type": "compaction", "compact_threshold": ...}]` は Responses API の各リクエストとともに送信され、レンダリングされたコンテキストがしきい値を超えると、API はレスポンスの一部として圧縮項目を出力できます。`OpenAIResponsesCompactionSession` はターン間でスタンドアロンの `responses.compact` エンドポイントを呼び出し、ローカルのセッション履歴を書き換えます。

### `extra_args` の受け渡し {#passing-extra_args}

SDK がまだトップレベルで直接公開していない、プロバイダー固有または新しいリクエストフィールドが必要な場合は、`extra_args` を使用します。

OpenAI モデルを使用する場合、`extra_args` を使用して、Responses API と Chat Completions API の両方にオプションのパラメーターを渡せます。たとえば、`user` と `service_tier` です。サポートされているモデルでは、[高速モード](https://developers.openai.com/api/docs/guides/fast-mode)を使用するために `extra_args={"service_tier": "fast"}` を設定します。`"priority"` も引き続き同等です。同じリクエストフィールドを、直接指定する `ModelSettings` フィールドでも設定しないでください。

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(
        temperature=0.1,
        extra_args={"service_tier": "flex", "user": "user_12345"},
    ),
)
```

## モデル呼び出しのタイムアウト {#model-call-timeouts}

各モデル呼び出し試行を制限するには、[`ModelSettings.timeout`][agents.model_settings.ModelSettings.timeout] を正の秒数に設定します。タイムアウトはストリーミング呼び出しと非ストリーミング呼び出しの両方に適用され、転送待機を含む試行全体が対象です。エージェント実行全体、関数ツールの実行、再試行のバックオフは制限されません。

```python
from agents import Agent, ModelSettings

agent = Agent(
    name="Assistant",
    model_settings=ModelSettings(timeout=30.0),
)
```

試行が制限時間を超えた場合、SDK は試行をキャンセルし、そのクリーンアップが完了するのを待ってから [`ModelTimeoutError`][agents.exceptions.ModelTimeoutError] を発生させます。Runner が管理する再試行が有効な場合、SDK は `context.normalized.is_timeout` を `True` に設定して、タイムアウトによる失敗を再試行ポリシーへ渡します。たとえば、`retry_policies.network_error()` はこの分類に一致します。許可された再試行ごとに、新しい試行単位のタイムアウトが適用されます。SDK は再試行前に、通常の[再実行安全性ルール](#safety-boundaries)も適用します。

## Runner が管理する再試行 {#runner-managed-retries}

再試行は実行時専用で、オプトイン方式です。`ModelSettings(retry=...)` を設定し、再試行ポリシーで再試行を選択しない限り、SDK は一般的なモデルリクエストを再試行しません。

Responses WebSocket 転送では、`retry_policies.provider_suggested()` がレスポンス前の過負荷フレームとコードのない `server_error` フレームを再試行の提案として認識します。これだけで再試行が有効になるわけではありません。引き続き `ModelRetrySettings` が必要であり、通常の再実行安全性チェックも適用されます。レスポンスイベントがすでに 1 つでも到着している場合、SDK はリクエストを再実行しません。

```python
from agents import Agent, ModelRetrySettings, ModelSettings, retry_policies

agent = Agent(
    name="Assistant",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        retry=ModelRetrySettings(
            max_retries=4,
            backoff={
                "initial_delay": 0.5,
                "max_delay": 5.0,
                "multiplier": 2.0,
                "jitter": True,
            },
            policy=retry_policies.any(
                retry_policies.provider_suggested(),
                retry_policies.retry_after(),
                retry_policies.network_error(),
                retry_policies.http_status([408, 409, 429, 500, 502, 503, 504]),
            ),
        )
    ),
)
```

`ModelRetrySettings` には次の 3 つのフィールドがあります。

<div class="field-table" markdown="1">

| フィールド | 型 | 注記 |
| --- | --- | --- |
| `max_retries` | `int | None` | 初回リクエスト後に許可される再試行回数。 |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | ポリシーが明示的な遅延を返さずに再試行する場合のデフォルトの遅延方式。`backoff.max_delay` が上限を設定するのは、計算されたこのバックオフ遅延のみです。ポリシーが返す明示的な遅延や retry-after ヒントには上限を設定しません。 |
| `policy` | `RetryPolicy | None` | 再試行するかどうかを決定するコールバック。このフィールドは実行時専用で、シリアライズされません。 |

</div>

再試行ポリシーは、次の情報を持つ [`RetryPolicyContext`][agents.retry.RetryPolicyContext] を受け取ります。

- 試行回数を考慮した判断に使用できる `attempt` と `max_retries`。
- ストリーミング動作と非ストリーミング動作を分岐できる `stream`。
- raw の確認に使用する `error`。
- `status_code`、`retry_after`、`error_code`、`is_network_error`、`is_timeout`、`is_abort` などの `normalized` 情報。
- 基盤となるモデルアダプターが再試行の指針を提供できる場合の `provider_advice`。
- ポリシーの実行前に取得される、安定した再実行安全性情報としての `response_started`、`replay_safety`、`stateful_request`。`replay_safety` は `"safe"`、`"unsafe"`、`"unknown"` のいずれかです。リクエストが `previous_response_id` または `conversation_id` を使用する場合、`stateful_request` は true になります。

ポリシーは次のいずれかを返せます。

- 単純な再試行判断を表す `True`／`False`。
- 遅延の上書き、診断理由の添付、または範囲を限定した安全でない再実行の明示的な承認を行う場合は、[`RetryDecision`][agents.retry.RetryDecision]。

SDK は、`retry_policies` で既製のヘルパーを公開しています。

| ヘルパー | 動作 |
| --- | --- |
| `retry_policies.never()` | 常にオプトアウトします。 |
| `retry_policies.provider_suggested()` | 利用可能な場合、プロバイダーの再試行に関する指示に従います。 |
| `retry_policies.network_error()` | 一時的な転送エラーとタイムアウトに一致します。 |
| `retry_policies.http_status([...])` | 選択した HTTP ステータスコードに一致します。 |
| `retry_policies.retry_after()` | retry-after ヒントが利用可能な場合にのみ、その遅延を使用して再試行します。このヘルパーは retry-after 値を明示的なポリシー遅延として扱うため、`backoff.max_delay` はその上限を設定しません。 |
| `retry_policies.any(...)` | ネストされたポリシーのいずれかがオプトインした場合に再試行します。 |
| `retry_policies.all(...)` | ネストされたすべてのポリシーがオプトインした場合にのみ再試行します。 |

ポリシーを組み合わせる場合、プロバイダーが拒否と再実行安全性の承認を区別できるときにそれらを維持するため、`provider_suggested()` が最も安全な最初の構成要素です。

##### 安全性の境界 {#safety-boundaries}

一部の失敗は再試行されません。

- 中断エラー。
- 出力がすでに開始され、再実行が安全でなくなるストリーミング実行。
- Programmatic Tool Calling リクエストなど、ローカル側の副作用に関する独立した再実行拒否があるリクエスト。ただし、プロバイダーが独自に再実行を安全と判定した場合を除きます。

プロバイダーが安全でないと判定した失敗も、デフォルトではブロックされます。独立したローカル側の副作用に関する拒否がない非ストリーミングリクエストでは、アプリケーションは `RetryDecision(retry=True, approve_unsafe_replay=True)` を返すことで、プロバイダー側の再実行リスクを受け入れられます。この承認を与える前に、`context.response_started`、`context.replay_safety`、`context.stateful_request` を確認し、プロバイダー側の処理を繰り返しても問題ない場合にのみ承認してください。通常の `RetryDecision(retry=True)` が再実行保護を回避することはなく、`approve_unsafe_replay=True` ではストリーミングの再試行やローカルの副作用を許可できません。

`previous_response_id` または `conversation_id` を使用するステートフルなフォローアップリクエストでは、再実行の安全性が不明な場合、安全側に倒して失敗します。このようなリクエストでは、`network_error()` や `http_status([500])` など、プロバイダー以外の述語だけでは不十分です。通常は `retry_policies.provider_suggested()` を介して、プロバイダーから再実行安全性の承認を含めるか、前述のように、プロバイダーが安全でないと判定した非ストリーミングの失敗を明示的に承認してください。

##### Runner とエージェントのマージ動作 {#runner-and-agent-merge-behavior}

`retry` は、Runner レベルとエージェントレベルの `ModelSettings` 間でディープマージされます。

- エージェントは `retry.max_retries` のみを上書きしても、Runner の `policy` を継承できます。
- エージェントは `retry.backoff` の一部だけを上書きし、Runner の兄弟バックオフフィールドを維持できます。
- `policy` は実行時専用です。そのため、シリアライズされた `ModelSettings` は `max_retries` と `backoff` を保持しますが、コールバック自体は省略します。

より詳細なコード例については、[`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) および[アダプターを利用した再試行のコード例](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py)を参照してください。

## 非 OpenAI プロバイダーのトラブルシューティング {#troubleshooting-non-openai-providers}

### トレーシングクライアントのエラー 401 {#tracing-client-error-401}

トレーシング関連のエラーが発生する場合、トレースが OpenAI サーバーへアップロードされる一方で、OpenAI API キーが設定されていないことが原因です。この問題を解決するには、次の 3 つの方法があります。

1. トレーシングを完全に無効にします: [`set_tracing_disabled(True)`][agents.set_tracing_disabled]。
2. トレーシング用の OpenAI キーを設定します: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]。この API キーはトレースのアップロードにのみ使用され、[platform.openai.com](https://platform.openai.com/) で発行されたものである必要があります。
3. OpenAI 以外のトレースプロセッサーを使用します。[トレーシングのドキュメント](../tracing.md#custom-tracing-processors)を参照してください。

### Responses API のサポート {#responses-api-support}

SDK はデフォルトで Responses API を使用しますが、他の多くの LLM プロバイダーはまだこれをサポートしていません。その結果、404 などの問題が発生する場合があります。解決方法は次の 2 つです。

1. [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api] を呼び出します。これは、環境変数で `OPENAI_API_KEY` と `OPENAI_BASE_URL` を設定している場合に機能します。
2. [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] を使用します。コード例は[こちら](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/)にあります。

### Chat Completions の互換性オプション {#chat-completions-compatibility-options}

Chat Completions を介してルーティングする場合、SDK は、Chat Completions では送信できない Responses 専用フィールドを暗黙的に削除することで互換性を維持します。これには `previous_response_id`、`conversation_id`、Responses API の `prompt` フィールド、テキストのみではないツール出力などが含まれます。開発中にこれらの不一致を即座に失敗させる場合は、OpenAI プロバイダーで厳格な機能検証を有効にします。

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses=False,
    strict_feature_validation=True,
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

[`MultiProvider`][agents.MultiProvider] を使用する場合は、代わりに `openai_strict_feature_validation=True` を渡します。

OpenAI Chat Completions API は音声出力を返せますが、[`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] は現在、音声出力を Agents SDK の実行項目へ変換しません。非ストリーミングメッセージまたはストリーミングの差分に音声出力が含まれる場合、アダプターは部分的または空の実行結果を返す代わりに `AgentsException("Audio is not currently supported")` を発生させます。SDK が管理する音声ワークフローには、[Realtime エージェント](../realtime/guide.md)または[音声エージェント](../voice/quickstart.md)を使用してください。

ストリーミングまたは非ストリーミングの Chat Completions レスポンスが、アシスタントのテキスト、ツール呼び出し、拒否のいずれも生成せずに `finish_reason="length"` で終了した場合、アダプターは [`ModelBehaviorError`][agents.exceptions.ModelBehaviorError] を発生させます。SDK はこの空の実行結果を、コンテンツポリシーによる拒否ではなく、トークンまたは推論バジェットの枯渇として扱います。そのため、モデル拒否ハンドラーは実行されません。

一部の OpenAI 互換 Chat Completions プロバイダーは、SDK での増分処理に十分な信頼性がないチャンク形式で、ツール呼び出しの差分をストリーミングします。その場合は、ストリーミングされたツール呼び出しのバッファリングを有効にし、プロバイダーのストリームが完了した後にのみ SDK がツール呼び出しを出力するようにします。

```python
from agents import OpenAIProvider

provider = OpenAIProvider(
    use_responses=False,
    buffer_streamed_tool_calls=True,
)
```

[`MultiProvider`][agents.MultiProvider] では、`openai_buffer_streamed_tool_calls=True` を使用します。

### structured outputs のサポート {#structured-outputs-support}

一部のモデルプロバイダーは、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs)をサポートしていません。その場合、次のようなエラーが発生することがあります。

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

これは一部のモデルプロバイダーの制約です。JSON 出力はサポートしていても、出力に使用する `json_schema` を指定できません。現在この問題の修正に取り組んでいますが、JSON スキーマ出力をサポートするプロバイダーを利用することを推奨します。サポートされていない場合、不正な形式の JSON によってアプリケーションが頻繁に動作しなくなる可能性があります。

## プロバイダー間でのモデルの混在 {#mixing-models-across-providers}

モデルプロバイダー間の機能差を把握しておく必要があります。そうしないと、エラーが発生する可能性があります。たとえば、OpenAI は structured outputs、マルチモーダル入力、ホスト型のファイル検索と Web 検索をサポートしていますが、他の多くのプロバイダーはこれらの機能をサポートしていません。次の制限に注意してください。

- 対応していないプロバイダーへ、サポートされていない `tools` を送信しないでください
- テキスト専用モデルを呼び出す前に、マルチモーダル入力を除外してください
- 構造化 JSON 出力をサポートしていないプロバイダーは、無効な JSON を生成する場合があることに注意してください。

## サードパーティ製アダプター {#third-party-adapters}

SDK の組み込みプロバイダー統合ポイントだけでは不十分な場合にのみ、サードパーティ製アダプターを使用してください。この SDK で OpenAI モデルのみを使用する場合は、Any-LLM や LiteLLM ではなく、組み込みの [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] 経路を使用してください。サードパーティ製アダプターは、OpenAI モデルと非 OpenAI プロバイダーを組み合わせる場合や、アダプターのみが提供するプロバイダー対応またはルーティングが必要な場合に使用します。アダプターは SDK と上流のモデルプロバイダーの間に互換性レイヤーを追加するため、機能のサポート状況やリクエストのセマンティクスはプロバイダーによって異なる場合があります。SDK には現在、ベストエフォートのベータ版アダプター統合として Any-LLM と LiteLLM が含まれています。

### Any-LLM {#any-llm}

Any-LLM のサポートは、Any-LLM が管理するプロバイダー対応またはルーティングが必要な場合に向けて、ベストエフォートのベータ版として提供されています。

上流のプロバイダー経路によっては、Any-LLM は Responses API、Chat Completions 互換 API、またはプロバイダー固有の互換性レイヤーを使用する場合があります。

Any-LLM が必要な場合は、`openai-agents[any-llm]` をインストールし、[`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) または [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py) から始めてください。[`MultiProvider`][agents.MultiProvider] で `any-llm/...` のモデル名を使用するか、`AnyLLMModel` を直接インスタンス化するか、実行スコープで `AnyLLMProvider` を使用できます。モデルサーフェスを明示的に固定する必要がある場合は、`AnyLLMModel` の作成時に `api="responses"` または `api="chat_completions"` を渡します。

Any-LLM の Chat Completions 経路では、[`ModelSettings.extra_body`][agents.model_settings.ModelSettings.extra_body] はネストされた `extra_body` 引数のままです。Agents SDK はそのマッピングを Any-LLM のトップレベル呼び出し引数へマージしないため、プロバイダー固有のリクエスト本文フィールドは `extra_body` マッピング内に維持してください。

Any-LLM は引き続きサードパーティ製アダプターレイヤーであるため、プロバイダーの依存関係と機能差は SDK ではなく、上流の Any-LLM によって定義されます。上流プロバイダーが使用量メトリクスを返す場合、それらは自動的に伝播されます。ただし、ストリーミング対応 Chat Completions バックエンドでは、使用量チャンクを出力するために `ModelSettings(include_usage=True)` が必要な場合があります。structured outputs、ツール呼び出し、使用量レポート、Responses 固有の動作に依存する場合は、デプロイ予定のプロバイダーバックエンドを正確に検証してください。

### LiteLLM {#litellm}

LiteLLM のサポートは、LiteLLM 固有のプロバイダー対応またはルーティングが必要な場合に向けて、ベストエフォートのベータ版として提供されています。

LiteLLM が必要な場合は、`openai-agents[litellm]` をインストールし、[`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) または [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py) から始めてください。`litellm/...` のモデル名を使用するか、[`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] を直接インスタンス化できます。

LiteLLM アダプターを介してアクセスする一部のプロバイダーは、デフォルトでは SDK の使用量メトリクスを設定しません。使用量レポートが必要な場合は `ModelSettings(include_usage=True)` を渡し、structured outputs、ツール呼び出し、使用量レポート、アダプター固有のルーティング動作に依存する場合は、デプロイ予定のプロバイダーバックエンドを正確に検証してください。

LiteLLM がレスポンスオブジェクトに関する Pydantic シリアライザーの警告を出力する場合、LiteLLM アダプターをインポートする前に、SDK の互換性パッチを有効にできます。

```bash
export OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH=true
```

このパッチはデフォルトで無効になっており、`1` または `true` の値に対してのみ有効になります。これは LiteLLM の非公開ログヘルパーをラップすることで、特定の種類の LiteLLM レスポンスシリアライズ警告を抑制します。そのため、一般的なシリアライズ設定ではなく、対象を限定した回避策として扱ってください。LiteLLM の非公開 API に依存するため、LiteLLM をアップグレードする際は再度検証し、上流で警告が発生しなくなったら環境変数を削除してください。