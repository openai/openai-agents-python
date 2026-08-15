---
search:
  exclude: true
---
# テスト

SDK は、エージェントワークフロー、Sandbox セッション、Realtime セッション、Voice パイプライン向けに、決定論的でプロバイダーに依存しないテストユーティリティを提供します。これらのユーティリティはメモリ内で動作し、モデル、Sandbox プロバイダー、Realtime API へのリクエストを行わず、SDK が管理する正規化済みのやり取りを記録します。以下の実行可能なレシピでは、OpenAI API キーが設定されている場合にデフォルトのトレースプロセッサーがテストアクティビティをアップロードしないよう、実行ごとにトレーシングを無効にしています。

これらは、アプリケーションと SDK が管理するオーケストレーション（ツール実行、ハンドオフ、ガードレール、再試行、ストリーミング、セッション動作、Sandbox 機能、Realtime イベント処理、Voice パイプライン構成）のテストに使用します。外部のモデル、ネットワークプロトコル、Sandbox プロバイダー、音声システムが管理する動作については、実際のプロバイダーアダプターまたは統合環境を使用してください。

## 必要なレシピの検索

| 目的 | 使用するもの | 参照先 |
| --- | --- | --- |
| 固定の最終回答を返す | `ScriptedModel` と `assistant_message()` | [固定レスポンスの返却](#return-a-fixed-response) |
| 複数ターンのツールループを実行する | `function_call()` の後にアシスタントレスポンス | [ツールワークフローのテスト](#test-a-tool-workflow) |
| リクエストからレスポンスを選択する | `ModelStep.respond()` または `responder` のマッピング | [リクエストからのレスポンス導出](#derive-a-response-from-the-request) |
| ランナーがモデルに送信した内容をアサートする | `calls`、`first_call`、または `last_call` | [モデル呼び出しの検査](#inspect-model-calls) |
| ストリーミング実行をテストする | 通常のレスポンスステップ、またはイベントを厳密に指定する `ModelStep.stream()` | [ストリーミングのテスト](#test-streaming) |
| エラーまたは再試行の判断をテストする | `ModelStep.raise_error()` | [モデル障害の注入](#inject-model-failures) |
| 意図しないワークフロー変更を検出する | 厳密な FIFO ステップと `assert_complete()` | [ワークフローのドリフト検出](#detect-workflow-drift) |
| Sandbox を起動せずに `SandboxAgent` をテストする | `scripted_sandbox_session()` と `ScriptedModel` | [Sandbox エージェントワークフローのテスト](#test-a-sandbox-agent-workflow) |
| Sandbox 呼び出しを照合する、またはその実行結果を導出する | Sandbox ステップの `match` または `responder` | [Sandbox ステップの設定](#configure-sandbox-steps) |
| 接続を開かずに Realtime セッションをテストする | `ScriptedRealtimeModel` と `RealtimeStep` | [Realtime セッションのテスト](#test-a-realtime-session) |
| Realtime ツールワークフローをテストする | `RealtimeModelToolCallEvent` を発行し、ツール出力を期待する | [Realtime ツールワークフローのテスト](#test-a-realtime-tool-workflow) |
| 静的またはストリーミングの Voice パイプラインをテストする | `ScriptedSTTModel`、`ScriptedTTSModel`、およびスクリプト化された、または実際のワークフロー | [Voice パイプラインのテスト](#test-a-voice-pipeline) |
| プロバイダーのシリアライズまたはワイヤーペイロードをテストする | 制御されたネットワークトランスポートを備えた実際のプロバイダーアダプター | [適切な境界の選択](#choose-the-correct-boundary) |

## インポート

テスト API は、置き換えるランタイム境界の近くに配置されています。

| 境界 | インポートパス |
| --- | --- |
| エージェントモデルと Sandbox ワークフロー | `agents.testing` |
| Realtime モデルトランスポート | `agents.realtime.testing` |
| Voice の STT、TTS、およびワークフローコンポーネント | `agents.voice.testing` |

テスト用シンボルは、意図的にトップレベルの `agents` インポートには含まれていません。

## エージェントワークフローのレシピ

### 固定レスポンスの返却

想定されるモデル呼び出しごとに、正規化済み出力項目のシーケンスを 1 つ渡します。出力シーケンスの省略記法には、1 回のリクエスト用の決定論的なレスポンス ID と使用量が設定されます。

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message


@pytest.mark.asyncio
async def test_fixed_response() -> None:
    model = ScriptedModel(
        [[assistant_message("Paris is the capital of France.")]]
    )
    agent = Agent(name="Geography assistant", model=model)

    result = await Runner.run(
        agent,
        "What is the capital of France?",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "Paris is the capital of France."
    assert len(model.calls) == 1
    model.assert_complete()
```

決定論的なワークフローテストの最後に `model.assert_complete()` を使用してください。設定されたすべてのステップを消費する前にワークフローが停止した場合を検出できます。

### ツールワークフローのテスト

ツールを呼び出すモデルレスポンスを 1 つ、その後に最終回答を生成する 2 つ目のレスポンスをスクリプト化します。これらのモデル呼び出しの間では、実際の SDK ツールパイプラインが実行されます。

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.testing import ScriptedModel, assistant_message, function_call


@tool
def get_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"{city}: sunny"


@pytest.mark.asyncio
async def test_tool_workflow() -> None:
    model = ScriptedModel(
        [
            [function_call("get_weather", {"city": "Tokyo"}, call_id="call_1")],
            [assistant_message("It is sunny in Tokyo.")],
        ]
    )
    agent = Agent(name="Weather assistant", model=model, tools=[get_weather])

    result = await Runner.run(
        agent,
        "What is the weather in Tokyo?",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "It is sunny in Tokyo."
    assert len(model.calls) == 2
    assert model.last_call is not None
    assert any(
        item.get("type") == "function_call_output"
        for item in model.last_call.input
    )
    model.assert_complete()
```

このパターンは、ツール入力の検証、実行、実行結果の変換、フック、ガードレール、および次のモデルターンをカバーします。Python 関数を直接呼び出すと、これらの SDK の動作は迂回されます。

### リクエストからのレスポンス導出

レスポンスが正規化済みモデル呼び出しに実際に依存する場合、またはアサーションをモデル境界に配置する場合は、`ModelStep.respond()` を使用します。レスポンダーは同期または非同期にでき、`ScriptedModel` が受け付ける任意のステップ形式を返せます。

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message


def respond(call: ModelCall):
    assert call.streamed is False
    assert call.input == [{"content": "Summarize this", "role": "user"}]
    return {"output": [assistant_message("Handled the normalized request.")]}


@pytest.mark.asyncio
async def test_request_aware_response() -> None:
    model = ScriptedModel([ModelStep.respond(respond)])
    agent = Agent(name="Assistant", model=model)

    result = await Runner.run(
        agent,
        "Summarize this",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "Handled the normalized request."
    model.assert_complete()
```

`ScriptedModel` は、`ModelStep`、同等の辞書形式、`ModelResponse`、正規化済み出力項目のシーケンス、または例外を受け付けます。レスポンスが呼び出しに依存しない場合は、固定スクリプトの方が予期しないターンを診断しやすいため、固定の出力シーケンスを優先してください。

### モデル呼び出しの検査

`ScriptedModel` は、選択されたステップを解決するか例外を発生させる前に、各呼び出しを記録します。

| メンバー | 内容 |
| --- | --- |
| `calls` | 呼び出し順のすべての `ModelCall` |
| `first_call` | 最初の呼び出し、または `None` |
| `last_call` | 最新の呼び出し、または `None` |
| `remaining_steps` | まだ消費されていない設定済みステップの数 |

一般的なアサーションには、`call.input`、`call.model_settings`、`call.tools`、`call.handoffs`、および `call.streamed` があります。可変のリクエストデータは呼び出し境界でスナップショットされ、公開されている各履歴アクセサーは、切り離されたスナップショットを返します。ツール、ハンドオフ、出力スキーマ、およびトレーシングのオブジェクトは、ランタイム上の同一性を維持します。

構造化された `call_index` および `input_index` のエラーフィールドは 0 始まりであるため、`calls[...]` または指定されたステップシーケンスのインデックスとして直接使用できます。人が読めるエラーメッセージでは、呼び出し番号またはステップ番号が 1 始まりで表示されます。

1 つのテストでモデルステップを段階的に追加する必要がある場合は、`enqueue()` または `extend()` を使用します。独立したシナリオには、新しい `ScriptedModel` を作成してください。このユーティリティは、消費済みステップや呼び出し履歴をリセットしません。

### ストリーミングのテスト

通常のレスポンスステップは、`Runner.run()` と `Runner.run_streamed()` の両方をサポートします。一般的なアシスタントメッセージ、推論項目、関数呼び出し、およびパッチ適用呼び出しについて、`ScriptedModel` は、正規化済みの開始、差分、項目完了、および終了レスポンスイベントを生成します。終了レスポンスには、完全な出力と使用量が含まれます。

正規化済み `TResponseStreamEvent` の厳密なシーケンスがテスト対象の動作に含まれる場合にのみ、`ModelStep.stream()` を使用してください。

```python
step = ModelStep.stream(
    events,
    output=[assistant_message("The terminal output used by the runner.")],
)
```

`events` には、固定シーケンス、または記録された `ModelCall` を受け取る非同期ファクトリーを指定できます。同じステップが非ストリーミング呼び出しで使用された場合に返されるレスポンスは、オプションの `output` です。厳密なストリームイベントは SDK で正規化されたイベントであり、Responses API や Chat Completions のワイヤーチャンクではありません。

自動ストリーミングでは、段階的なライフサイクルが実装されていない種類の正規化済み出力項目は拒否されます。不完全なイベントシーケンスに依存せず、それらの項目には `ModelStep.stream(...)` を使用してください。

### モデル障害の注入

1 回のモデル呼び出しを失敗させるには、`ModelStep.raise_error()` を使用します。オプションの再試行に関する指示は、そのスクリプト化されたエラーにのみ適用されます。

```python
from agents import ModelRetryAdvice
from agents.testing import ModelStep


step = ModelStep.raise_error(
    RuntimeError("temporary failure"),
    retry_advice=ModelRetryAdvice(suggested=True, replay_safety="safe"),
)
```

ランナーの再試行ポリシーが、その指示によって再試行するかどうかを決定します。各再試行は別のモデル呼び出しであり、次のスクリプト化されたステップを消費します。Python ヘルパーは固定の `ModelRetryAdvice` 値を受け付けます。再試行に関する指示自体を試行ごとに動的に変える必要がある場合は、カスタム `Model` を使用してください。

### ワークフローのドリフト検出

スクリプト化された呼び出しを、想定されるワークフロー形状として扱います。余分なモデルリクエストがあると `UnexpectedModelCall` が発生し、早期終了するとステップが残り、`assert_complete()` によって報告されます。

テストフレームワークがティアダウンまたはファイナライザーをサポートしており、別のアサーションが失敗した後にも未消費ステップを報告したい場合は、そこに `assert_complete()` を配置してください。通常の回帰テストでは、不一致エラーをキャッチしないでください。

| エラー | 構造化フィールド | 意味 |
| --- | --- | --- |
| `InvalidModelStep` | `reason`、`input_index` | ステップの形式が不正であり、キューに入る前に拒否されました |
| `UnexpectedModelCall` | `call`、`call_index` | スクリプトの終了後にワークフローが別のモデル呼び出しを行いました |
| `UnconsumedModelSteps` | `remaining_steps` | すべてのステップを使用する前にワークフローが終了しました |

## Sandbox エージェントのレシピ

### Sandbox エージェントワークフローのテスト

`ScriptedModel` と `scripted_sandbox_session()` を組み合わせると、ローカルコンテナまたはリモート Sandbox を作成せずに、実際の `SandboxAgent` ランタイムを実行できます。モデルスクリプトは機能ツールを選択し、Sandbox スクリプトは対応する `SandboxSession` メソッドが返す内容を定義します。

```python
import pytest

from agents import RunConfig, Runner
from agents.sandbox import ExecResult, SandboxAgent
from agents.sandbox.capabilities import Shell
from agents.testing import (
    ScriptedModel,
    assistant_message,
    function_call,
    scripted_sandbox_session,
)


@pytest.mark.asyncio
async def test_sandbox_workflow() -> None:
    sandbox = scripted_sandbox_session(
        [
            {
                "method": "exec",
                "match": lambda call: call.args == ("pwd",),
                "result": ExecResult(
                    stdout=b"/workspace\n",
                    stderr=b"",
                    exit_code=0,
                ),
            }
        ]
    )
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "pwd"}, call_id="call_1")],
            [assistant_message("The workspace is /workspace.")],
        ]
    )
    agent = SandboxAgent(
        name="Workspace assistant",
        model=model,
        capabilities=[Shell()],
    )

    async with sandbox:
        result = await Runner.run(
            agent,
            "Which directory are you in?",
            run_config=RunConfig(
                sandbox={"session": sandbox},
                tracing_disabled=True,
            ),
        )

    assert result.final_output == "The workspace is /workspace."
    assert [call.method for call in sandbox.calls] == ["exec"]
    sandbox.assert_complete()
    model.assert_complete()
```

このテストは、正規化された 2 つの SDK 境界を通過します。ツール引数の検証、機能のルーティング、Sandbox セッションの呼び出し、次のモデルターンへのツール実行結果の受け渡し、および最終出力の処理をカバーします。実際のモデルがコマンドを選択するかどうかや、実際の Sandbox プロバイダーがそれをどのように実行するかはテストしません。

### Sandbox ステップの設定

一致する各 Sandbox 呼び出しは、1 つのグローバル FIFO シーケンスから次のステップを消費します。メソッドの不一致、マッチャーによる拒否、またはマッチャーの例外が発生した場合、そのステップは保留中のままになります。`method` を設定し、結果を厳密に 1 つ選択し、呼び出しの詳細が重要な場合にのみ `match` を追加してください。

| ステップメンバー | 使用する場合 |
| --- | --- |
| `result` | メソッドが固定の型付き値を返す場合 |
| `responder` | 実行結果が切り離された `SandboxCall` に依存する場合 |
| `error` | メソッドが特定の例外を発生させる場合 |
| `match` | マッチャーが `False` 以外の値を返さない限り、結果を生成する前に呼び出しを拒否する場合 |

サポートされるスクリプト化メソッド名は、`apply_patch`、`exec`、`ls`、`mkdir`、`pty_exec_start`、`pty_write_stdin`、`read`、`rm`、および `write` です。設定されたモデル向け機能のみが公開されます。2 つの PTY メソッドは 1 つの対話型シェル機能を構成するため、いずれかの PTY メソッドが設定されると、両方がまとめて公開されます。ただし、呼び出しは引き続きグローバル FIFO スクリプトを消費します。

`sandbox.calls` には、0 始まりの `call_index`、`method`、位置引数の `args`、および読み取り専用の `kwargs` を持つ、切り離された `SandboxCall` スナップショットが含まれます。静的な実行結果も、スクリプトの作成時にスナップショットされます。`io.BytesIO` と `io.StringIO` の値がサポートされています。その他のライブストリームオブジェクトまたはライフサイクル動作には、カスタム Sandbox セッションを使用してください。

| エラー | 構造化フィールド | 意味 |
| --- | --- | --- |
| `InvalidSandboxStep` | `reason`、`input_index`、`method` | ステップの形式が不正であるか、サポートされていないメソッド名が指定されています |
| `UnexpectedSandboxCall` | `call`、`call_index`、`actual_method`、`expected_method`、`remaining_steps` | ワークフローが誤ったメソッドを呼び出したか、スクリプトの終了後も続行しました |
| `SandboxCallMatcherError` | `call`、`call_index`、`method` | ステップマッチャーが `False` を返しました |
| `UnconsumedSandboxSteps` | `remaining_steps`、`pending_methods` | すべてのステップを使用する前にワークフローが終了しました |

返されるオブジェクトはセッション自体です。`RunConfig(sandbox={"session": sandbox})` に直接渡してください。ラッパーの `.session` 属性はありません。

## Realtime のレシピ

### Realtime セッションのテスト

`ScriptedRealtimeModel` は、Python SDK の正規化済み `RealtimeModel` 境界を実装します。各 `RealtimeStep` は、1 つの送信 `RealtimeModelSendEvent` と照合し、その後、正規化済みの受信 `RealtimeModelEvent` オブジェクトを発行するか、注入されたエラーを発生させます。

```python
import pytest

from agents.realtime import (
    RealtimeAgent,
    RealtimeModelOutputTextDeltaEvent,
    RealtimeModelSendUserInput,
    RealtimeRawModelEvent,
    RealtimeRunner,
)
from agents.realtime.testing import RealtimeStep, ScriptedRealtimeModel


@pytest.mark.asyncio
async def test_realtime_message() -> None:
    reply = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="Hello!",
        response_id="response_1",
    )
    model = ScriptedRealtimeModel(
        [
            RealtimeStep(
                expect=RealtimeModelSendUserInput(user_input="Hello"),
                emit=[reply],
            )
        ]
    )
    runner = RealtimeRunner(
        RealtimeAgent(name="Assistant"),
        model=model,
        config={"tracing_disabled": True},
    )

    observed_reply = False
    async with await runner.run() as session:
        await session.send_message("Hello")
        async for event in session:
            if isinstance(event, RealtimeRawModelEvent) and event.data == reply:
                observed_reply = True
                break

    assert observed_reply
    assert model.sent_events == (RealtimeModelSendUserInput(user_input="Hello"),)
    assert model.closed is True
    model.assert_complete()
```

期待値には、厳密なイベント値、`isinstance` で照合されるイベントクラス、または送信イベントを受け取り、一致した場合に `True` を返す callable を指定できます。デフォルトでは厳格モードが有効です。`strict=False` を使用すると、無関係な送信イベントは記録されますが、保留中のステップは消費されません。これは、テスト対象の動作に含まれない付随的なイベントをセッションが発行する場合に便利です。

接続中に受信イベントを発行するには、`connect_events` を使用します。ライフサイクルの障害には `connect_error` または `close_error` を使用し、1 回の照合済み送信に関連付けられた障害には `RealtimeStep(error=...)` を使用します。1 つのステップに `emit` と `error` の両方を定義することはできません。

### Realtime ツールワークフローのテスト

実際の関数ツールを `RealtimeAgent` に接続し、正規化済みツール呼び出しを発行して、SDK がモデル境界を通じてツール出力を送信することを期待します。`async_tool_calls` を `False` に設定すると、この小さなコード例は、テスト専用の待機機構を使用せずに接続中に完了します。

```python
import pytest

from agents.decorators import tool
from agents.realtime import (
    RealtimeAgent,
    RealtimeModelSendToolOutput,
    RealtimeModelToolCallEvent,
    RealtimeRunner,
)
from agents.realtime.testing import RealtimeStep, ScriptedRealtimeModel


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by ID."""
    return f"Order {order_id} has shipped."


@pytest.mark.asyncio
async def test_realtime_tool_workflow() -> None:
    tool_call = RealtimeModelToolCallEvent(
        name="lookup_order",
        call_id="call_1",
        arguments='{"order_id":"order_123"}',
    )

    def matches_tool_output(event) -> bool:
        return (
            isinstance(event, RealtimeModelSendToolOutput)
            and event.tool_call.call_id == "call_1"
            and event.output == "Order order_123 has shipped."
        )

    model = ScriptedRealtimeModel(
        [RealtimeStep(expect=matches_tool_output)],
        connect_events=[tool_call],
    )
    agent = RealtimeAgent(
        name="Order assistant",
        tools=[lookup_order],
    )
    runner = RealtimeRunner(
        agent,
        model=model,
        config={"async_tool_calls": False, "tracing_disabled": True},
    )

    async with await runner.run():
        pass

    model.assert_complete()
```

これにより、実際の Realtime ツール検索、引数検証、実行、および出力ルーティングが実行されます。実際のモデルがツールを選択することを証明するものではありません。

### Realtime の呼び出しとライフサイクルの検査

| メンバー | 内容 |
| --- | --- |
| `connect_calls` | 認証情報を含まない、切り離された接続スナップショット |
| `sent_events` | 呼び出し順の、切り離された送信イベントのスナップショット |
| `remaining_steps` | 残っている想定送信 |
| `listeners` | 現在登録されているリスナーオブジェクト |
| `connected`、`closed`、`close_calls` | 現在のメモリ内ライフサイクル状態 |

接続履歴には、API キーまたはヘッダーフィールドが指定されたかどうかのみが記録され、その値は保存されません。URL のスナップショットからは、ユーザー情報、クエリパラメーター、およびフラグメントが削除されます。可変のイベントデータと設定は切り離されますが、ツール、ハンドオフ、再生トラッカーなどのライブ SDK オブジェクトは同一性を維持します。

最後に `model.assert_complete()` を使用し、`RealtimeSession` 非同期コンテキストマネージャーによってモデルを閉じてください。Python ユーティリティは、保留中の期待値を表す Promise、暗黙的なタイムアウト、または個別の `assert_closed()` ヘルパーを意図的に提供していません。

| エラー | 構造化フィールド | 意味 |
| --- | --- | --- |
| `UnexpectedRealtimeSend` | `actual`、`expected` | 厳格な送信が次のステップと一致しなかったか、ステップが残っていませんでした |
| `UnconsumedRealtimeSteps` | `remaining_steps` | 想定されたすべての送信を使用する前にセッションが終了しました |
| `RealtimeScriptError` | なし | 切断中の送信など、無効なライフサイクル状態でスクリプトが使用されました |

## Voice パイプラインのレシピ

### Voice パイプラインのテスト

スクリプト化された STT および TTS モデルを、`SingleAgentVoiceWorkflow` と `ScriptedModel` を基盤とするエージェントと組み合わせると、プロバイダーへのリクエストを行わずに、音声テキスト変換 -> エージェント -> テキスト音声変換のパイプライン全体をテストできます。

```python
import numpy as np
import pytest

from agents import Agent
from agents.testing import ScriptedModel, assistant_message
from agents.voice import AudioInput, SingleAgentVoiceWorkflow, VoicePipeline
from agents.voice.testing import (
    ScriptedSTTModel,
    ScriptedTTSModel,
    TTSResult,
    pcm16_samples,
)


@pytest.mark.asyncio
async def test_voice_pipeline() -> None:
    model = ScriptedModel([[assistant_message("Hello there.")]])
    stt = ScriptedSTTModel("hello")
    pcm = pcm16_samples([0, 100, -100, 0])
    tts = ScriptedTTSModel([TTSResult([pcm])])
    pipeline = VoicePipeline(
        workflow=SingleAgentVoiceWorkflow(
            Agent(name="Voice assistant", model=model)
        ),
        stt_model=stt,
        tts_model=tts,
        config={"tracing_disabled": True, "tts_settings": {"buffer_size": 1}},
    )

    result = await pipeline.run(AudioInput(np.zeros(2, dtype=np.int16)))
    events = [event async for event in result.stream()]

    assert events
    assert [call.text for call in tts.calls] == ["Hello there."]
    stt.assert_complete()
    tts.assert_complete()
    model.assert_complete()
```

パイプラインの STT/TTS ライフサイクルがテスト対象で、エージェントオーケストレーションが対象ではない場合は、代わりに `ScriptedVoiceWorkflow` を使用してください。

```python
from agents.voice.testing import ScriptedVoiceWorkflow


workflow = ScriptedVoiceWorkflow(
    turns=["Hello there."],
    start="Welcome.",
)
```

`start` ステップは、`on_start()` によって消費されます。`VoicePipeline` が `on_start()` を呼び出すのは `StreamedAudioInput` の場合のみです。静的な `AudioInput` 実行では、`start` は消費されません。通常の各ターンでは、文字起こしが記録され、設定された実行結果が 1 つ消費されます。文字列は 1 つのフラグメントです。文字列のシーケンスでは、テキスト分割と TTS の前のフラグメント境界を制御できます。

### ストリーミング文字起こしのテスト

`ScriptedSTTModel` は、静的な `transcriptions` と、個別にスクリプト化されたストリーミング `sessions` を受け付けます。セッションには、`ScriptedTranscriptionSession`、文字起こしターンのシーケンス、例外、または単一の文字列を指定できます。

```python
from agents.voice.testing import ScriptedSTTModel, ScriptedTranscriptionSession


session = ScriptedTranscriptionSession(["first turn", "second turn"])
stt = ScriptedSTTModel(sessions=[session])
```

`ScriptedTranscriptionSession` を閉じると反復が停止し、スキップされたターンは `assert_complete()` による報告対象として残ります。同様に、`ScriptedTTSModel` は、呼び出しごとに 1 つの `TTSResult`、バイトチャンクのシーケンス、または例外を消費します。

### Voice 呼び出しの検査

| コンポーネント | 記録される履歴 |
| --- | --- |
| `ScriptedSTTModel` | `calls`、`session_calls`、およびライブ `created_sessions` の同一性 |
| `ScriptedTTSModel` | テキストと切り離された設定を含む `calls` |
| `ScriptedVoiceWorkflow` | ターン順の `transcriptions` |

静的な音声バッファと可変の設定は、呼び出し時にスナップショットされます。`StreamedAudioInput` および作成された文字起こしセッションオブジェクトは、パイプラインが引き続き使用するため、ライブオブジェクトとしての同一性を維持します。

| エラー | 構造化フィールド | 意味 |
| --- | --- | --- |
| `UnexpectedVoiceCall` | `operation` | 静的な文字起こし、ストリーミングセッション、TTS 呼び出し、ワークフロー開始、またはワークフローターンに設定済みステップがありませんでした |
| `UnconsumedVoiceSteps` | `remaining_steps` | 1 つ以上の設定済み Voice ステップが残っています |

テストで設定するスクリプト化された各 Voice コンポーネントに対して、`assert_complete()` を呼び出してください。`ScriptedSTTModel.assert_complete()` は、それが作成した文字起こしセッション内のターンも確認します。

## 適切な境界の選択

モデルプロバイダーに依存せずに、SDK の実行ループ、ツール、ハンドオフ、ガードレール、セッション、再試行、または正規化済みストリーミングをテストする場合は、`ScriptedModel` を使用します。

Sandbox プロバイダーを起動せずに `SandboxAgent` の機能とオーケストレーションをテストする場合は、`ScriptedModel` とともに `scripted_sandbox_session()` を使用します。プロバイダーの作成、プロセス実行、ファイルシステムの忠実性、永続性、リソース制限、および分離の検証は、実際の Sandbox プロバイダーに対する統合テストで行ってください。

WebSocket 接続を開かずに `RealtimeSession` の動作、または `RealtimeAgent` のツールとハンドオフのオーケストレーションをテストする場合は、`ScriptedRealtimeModel` を使用します。raw Realtime クライアント／サーバーイベント、認証、ネットワーク復旧、および音声トランスポートの動作は、実際のトランスポートまたは統合環境でテストしてください。Realtime API セッションでは、クライアントが入力を送信してイベントを受信している間、接続を開いたままにします。そのため、これらのネットワークおよびプロトコルに関する事項は、正規化済みモデル境界より下位に属します。本番環境の接続アーキテクチャについては、[OpenAI Realtime API ガイド](https://developers.openai.com/api/docs/guides/realtime)を参照してください。

音声プロバイダーを使用せずに、STT/TTS の順序、ストリーミング文字起こしのクリーンアップ、ワークフローフラグメントの受け渡し、または Voice パイプライン全体の構成をテストする場合は、Voice テストコンポーネントを使用します。文字起こし品質、生成音声、エンコード互換性、レイテンシ、または再生がテスト対象の場合は、実際の音声モデルと代表的な音声を使用してください。

Responses API または Chat Completions のリクエストシリアライズ、認証ヘッダー、プロバイダーのデフォルト値、HTTP ペイロード、プロバイダーのストリームチャンク、Realtime ワイヤーフレーム、またはプロバイダー固有のライフサイクル動作のテストには、これらのユーティリティを使用しないでください。そのようなテストでは実際のアダプターを維持し、そのネットワーク境界を置き換えるか制御してください。`openai` v3 では、OpenAI アダプターのテストに `httpx2` のリクエスト、レスポンス、トランスポート、および例外の型を使用する必要があります。従来の `httpx` は、Agents SDK のコア依存関係ではありません。

## 最終チェックリスト

- 正規化済みモデル、Sandbox セッション、Realtime モデル、または Voice パイプライン境界が管理するやり取りのみをスクリプト化します。
- ランナーのプライベート状態ではなく、重要な公開リクエストフィールドまたは呼び出しフィールドをアサートします。
- 固定レスポンスステップを優先し、リクエスト依存の動作にのみレスポンダーを使用します。
- モデルの自動ストリーミングを優先し、イベントレベルの動作が重要な場合にのみ厳密なストリームを使用します。
- スクリプト化された各コンポーネントのテストを、その `assert_complete()` メソッドで終了します。
- 周囲のテストが Realtime および Sandbox のライフサイクルを管理する場合は、非同期コンテキストマネージャーを使用してライフサイクルをクリーンアップします。
- 人が読めるメッセージを解析するのではなく、構造化されたエラーフィールドをアサートします。
- プロバイダーのワイヤーテストでは、制御されたネットワークトランスポートを備えた実際のアダプターを使用します。

## スコープと現在の制限

テストモジュールは、意図的に以下を提供していません。

- 正規化済みモデル出力項目ごとの便利なビルダー。一般的なケースには `assistant_message()` と `function_call()` を使用し、その他の正規化済み項目は直接渡してください。
- プロバイダープロトコルのシミュレーター。厳密なモデルストリームでは、Responses API または Chat Completions のワイヤーチャンクではなく、正規化済み SDK イベントを使用します。
- 高レベルのシミュレートされた Realtime サーバー。テストでは、正規化済みの送信を明示的に照合し、シナリオに必要な正規化済み受信イベントを発行します。
- 順不同の Sandbox または Realtime の期待値。どちらのユーティリティも、1 つのグローバルな順序で想定ステップを消費します。
- テストランナー固有のマッチャー、フィクスチャ、暗黙的なタイムアウト、または自動ティアダウン。
- リセット API。`ScriptedModel` は段階的なスクリプト用の `enqueue()` と `extend()` をサポートしますが、独立したシナリオには新しいスクリプト化コンポーネントを作成してください。

不正な形式のストリーム、制御された中断または並行処理、厳密なキャンセル、またはスクリプト化ユーティリティでは維持できないライフサイクル境界がテストで必要な場合は、対応する公開インターフェースのカスタム実装を使用してください。その特殊な境界をテスト内に記載してください。

## API リファレンス

- [`agents.testing`](ref/testing.md)
- [`agents.realtime.testing`](ref/realtime/testing.md)
- [`agents.voice.testing`](ref/voice/testing.md)