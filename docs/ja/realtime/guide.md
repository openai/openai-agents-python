---
search:
  exclude: true
---
# Realtime エージェントガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を使用して音声対応 AI エージェントを構築する方法を詳しく説明します。

!!! warning "Beta 機能"
Realtime エージェントは beta です。実装の改善に伴い、互換性のない変更が発生する可能性があります。

## 概要

Realtime エージェントでは、オーディオとテキストの入力をリアルタイムに処理し、realtime オーディオで応答する対話フローが可能です。OpenAI の Realtime API との永続接続を維持することで、低遅延で自然な音声会話と、割り込みの適切な処理を実現します。

## アーキテクチャ

### コアコンポーネント

realtime システムは、いくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、ツール、ハンドオフで構成されるエージェントです。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出すとセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常はユーザーが会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルインターフェースです（通常は OpenAI の WebSocket 実装）。

### セッションフロー

一般的な realtime セッションは、次のフローに従います。

1. instructions、ツール、ハンドオフを設定して **RealtimeAgent を作成** します。
2. エージェントと設定オプションで **RealtimeRunner を設定** します。
3. `await runner.run()` を使って **セッションを開始** します。これにより RealtimeSession が返されます。
4. `send_audio()` または `send_message()` を使って、セッションに **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントを監視** します。イベントには、音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザーがエージェントの発話に重ねて話した際の **割り込みを処理** します。これにより現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと似ていますが、いくつか重要な違いがあります。API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API リファレンスを参照してください。

通常のエージェントとの主な違い:

-   モデル選択はエージェント単位ではなく、セッション単位で設定します。
-   structured outputs はサポートされません（`outputType` は未対応です）。
-   音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   ツール、ハンドオフ、instructions などのその他の機能は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの挙動を制御できます。モデル名（`gpt-realtime` など）、音声選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキストおよび/または音声）を設定できます。音声形式は入力と出力の両方で設定でき、既定は PCM16 です。

### オーディオ設定

オーディオ設定では、セッションが音声入力と出力をどのように処理するかを制御します。Whisper などのモデルを使った入力音声の文字起こし、言語設定、ドメイン固有用語の精度向上のための文字起こしプロンプトを設定できます。ターン検出設定では、エージェントがいつ応答を開始・停止するかを制御でき、音声アクティビティ検出のしきい値、無音時間、検出音声周辺のパディングなどを設定できます。

`RealtimeRunner(config=...)` で設定できる追加オプションは以下のとおりです。

-   `model_settings.output_modalities` で出力をテキストおよび/または音声に制限します。
-   `model_settings.input_audio_noise_reduction` で近接音声または遠距離音声向けのノイズ低減を調整します。
-   `guardrails_settings.debounce_text_length` で出力ガードレールの実行頻度を制御します。
-   `async_tool_calls` で関数ツールを並行実行します。
-   `tool_error_formatter` でモデルに表示されるツールエラーメッセージをカスタマイズします。

型付き設定の完全版は [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] と [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## ツールと関数

### ツール追加

通常のエージェントと同様に、realtime エージェントは会話中に実行される関数ツールをサポートします。

```python
from agents import function_tool

@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    # Your weather API logic here
    return f"The weather in {city} is sunny, 72°F"

@function_tool
def book_appointment(date: str, time: str, service: str) -> str:
    """Book an appointment."""
    # Your booking logic here
    return f"Appointment booked for {service} on {date} at {time}"

agent = RealtimeAgent(
    name="Assistant",
    instructions="You can help with weather and appointments.",
    tools=[get_weather, book_appointment],
)
```

## ハンドオフ

### ハンドオフ作成

ハンドオフにより、専門化されたエージェント間で会話を引き継げます。

```python
from agents.realtime import realtime_handoff

# Specialized agents
billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing and payment issues.",
)

technical_agent = RealtimeAgent(
    name="Technical Support",
    instructions="You handle technical troubleshooting.",
)

# Main agent with handoffs
main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="You are the main customer service agent. Hand off to specialists when needed.",
    handoffs=[
        realtime_handoff(billing_agent, tool_description="Transfer to billing support"),
        realtime_handoff(technical_agent, tool_description="Transfer to technical support"),
    ]
)
```

## ランタイム挙動とセッション処理

### イベント処理

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理することで監視できます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。処理すべき主なイベントは以下のとおりです。

-   **audio**: エージェント応答の raw オーディオデータ
-   **audio_end**: エージェントの発話終了
-   **audio_interrupted**: ユーザーがエージェントを中断
-   **tool_start/tool_end**: ツール実行ライフサイクル
-   **handoff**: エージェントのハンドオフ発生
-   **error**: 処理中のエラー発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

### ガードレール

realtime エージェントでは、出力ガードレールのみサポートされています。これらのガードレールは、リアルタイム生成中のパフォーマンス問題を避けるために、デバウンスされて定期的に（すべての単語ごとではなく）実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチすることも、セッションの `run_config` で提供することもできます。両方のソースのガードレールが一緒に実行されます。

```python
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail

def sensitive_data_check(context, agent, output):
    return GuardrailFunctionOutput(
        tripwire_triggered="password" in output,
        output_info=None,
    )

agent = RealtimeAgent(
    name="Assistant",
    instructions="...",
    output_guardrails=[OutputGuardrail(guardrail_function=sensitive_data_check)],
)
```

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できる場合があります。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取れます。テキストエージェントとは異なり、realtime エージェントはガードレールがトリガーされても **Exception** を発生させません。

### オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用してセッションに音声を送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントを監視し、任意の音声ライブラリで音声データを再生します。ユーザーがエージェントを中断した際に再生を即時停止し、キュー済み音声をクリアできるよう、`audio_interrupted` イベントを必ず監視してください。

## 高度な統合と低レベルアクセス

### SIP 統合

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 経由で着信した電話に realtime エージェントを接続できます。SDK は [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を提供しており、SIP 上でメディアネゴシエーションを行いながら同じエージェントフローを再利用します。

使用するには、runner にモデルインスタンスを渡し、セッション開始時に SIP の `call_id` を指定します。call ID は着信を通知する webhook により配信されます。

```python
from agents.realtime import RealtimeAgent, RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(
    starting_agent=agent,
    model=OpenAIRealtimeSIPModel(),
)

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
        "initial_model_settings": {
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        },
    },
) as session:
    async for event in session:
        ...
```

発信者が通話を終了すると、SIP セッションは終了し、realtime 接続は自動的に閉じられます。電話連携の完全な例は [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

### モデル直接アクセス

カスタムリスナーの追加や高度な操作の実行のために、基盤モデルへアクセスできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続のより低レベルな制御が必要な高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

### 例と追加資料

完全に動作する例については、UI コンポーネントあり・なしのデモを含む [examples/realtime directory](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を確認してください。

### Azure OpenAI エンドポイント形式

Azure OpenAI に接続する場合は、GA Realtime エンドポイント形式を使用し、`model_config` の
headers 経由で認証情報を渡してください。

```python
model_config = {
    "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
    "headers": {"api-key": "<your-azure-api-key>"},
}
```

トークンベース認証では、`headers` に `{"authorization": f"Bearer {token}"}` を使用してください。