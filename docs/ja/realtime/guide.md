---
search:
  exclude: true
---
# Realtime エージェントガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を使用して音声対応 AI エージェントを構築する方法を、詳しく解説します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。実装を改善する過程で、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントにより、音声とテキストの入力をリアルタイムに処理し、realtime 音声で応答する対話フローを実現できます。OpenAI の Realtime API との永続接続を維持し、低レイテンシの自然な音声会話と、割り込みを適切に扱う機能を提供します。

## アーキテクチャ

### 中核コンポーネント

realtime システムは、いくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: `instructions`、`tools`、`handoffs` で構成されたエージェントです。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常、ユーザーが会話を開始するたびに 1 つ作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルのインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

典型的な realtime セッションは、次のフローに従います。

1. `instructions`、`tools`、`handoffs` を指定して **RealtimeAgent を作成**します。
2. エージェントと設定オプションで **RealtimeRunner をセットアップ**します。
3. `await runner.run()` を使って **セッションを開始**します。戻り値は RealtimeSession です。
4. `send_audio()` または `send_message()` を使って、セッションへ **音声またはテキストメッセージを送信**します。
5. セッションを反復して **イベントを購読**します。イベントには、音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザーがエージェントの発話にかぶせて話した場合に **割り込みを処理**します。これにより現在の音声生成が自動的に停止します。

このセッションは会話履歴を維持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスを参照してください。

通常のエージェントとの主な違い:

-   モデル選択は、エージェント単位ではなくセッション単位で設定します。
-   structured output のサポートはありません（`outputType` はサポートされません）。
-   音声はエージェントごとに設定できますが、最初のエージェントが話した後は変更できません。
-   それ以外の `tools`、`handoffs`、`instructions` などの機能は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの挙動を制御できます。モデル名（`gpt-realtime` など）、音声（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方で設定でき、既定は PCM16 です。

### 音声設定

音声設定は、セッションが音声入力と出力をどのように扱うかを制御します。Whisper などのモデルを使用した入力音声の文字起こし、言語の優先設定、専門用語の精度向上のための文字起こしプロンプトを設定できます。ターン検出設定では、エージェントがいつ応答を開始・停止すべきかを制御し、音声活動検出のしきい値、無音時間、検出された発話の前後のパディングなどのオプションがあります。

`RealtimeRunner(config=...)` で設定できる追加オプションには、次が含まれます。

-   `model_settings.output_modalities` で、出力をテキストおよび/または音声に制約します。
-   `model_settings.input_audio_noise_reduction` で、近距離/遠距離音声向けのノイズ低減を調整します。
-   `guardrails_settings.debounce_text_length` で、出力ガードレールの実行頻度を制御します。
-   `async_tool_calls` で、関数ツールを並行実行します。
-   `tool_error_formatter` で、モデルに見えるツールのエラーメッセージをカスタマイズします。

型付きの設定全体については、[`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] と [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## ツールと関数

### ツールの追加

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

### ハンドオフの作成

ハンドオフにより、専門化されたエージェント間で会話を転送できます。

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

## 実行時挙動とセッション処理

### イベント処理

セッションはイベントをストリーミングし、セッションオブジェクトを反復することでそれらを受け取れます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。処理すべき主要イベントは次のとおりです。

-   **audio**: エージェント応答からの raw 音声データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザーがエージェントを割り込み
-   **tool_start/tool_end**: ツール実行ライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細については、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

### ガードレール

realtime エージェントでは、出力ガードレールのみがサポートされます。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために、（毎単語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` 経由で提供できます。両方のソースからのガードレールはまとめて実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンス挙動は、安全性とリアルタイム性能要件のバランスに役立ちます。テキストエージェントとは異なり、realtime エージェントではガードレールがトリップしても Exception を **発生させません**。

### 音声処理

音声は [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用してセッションに送信するか、テキストは [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用して送信します。

音声出力については、`audio` イベントを購読し、好みの音声ライブラリで音声データを再生します。ユーザーがエージェントを割り込んだ場合に即座に再生を停止し、キューに入った音声をクリアするために、`audio_interrupted` イベントを必ず購読してください。

## 高度な統合と低レベルアクセス

### SIP 統合

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 経由で着信する電話に、realtime エージェントを接続できます。SDK は [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を提供しており、SIP 上でメディアをネゴシエートしつつ同じエージェントフローを再利用します。

使用するには、runner にモデルインスタンスを渡し、セッション開始時に SIP の `call_id` を指定します。call ID は、着信を通知する webhook によって配信されます。

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

発信者が通話を終了すると SIP セッションが終了し、realtime 接続は自動的にクローズされます。完全な電話連携の例は、[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

### モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタムリスナーを追加したり、高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

### 例と追加資料

完全に動作する例については、UI コンポーネントの有無を含むデモが入った [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を確認してください。

### Azure OpenAI エンドポイント形式

Azure OpenAI に接続する場合は、GA Realtime のエンドポイント形式を使用し、`model_config` の `headers` 経由で資格情報を渡します。

```python
model_config = {
    "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
    "headers": {"api-key": "<your-azure-api-key>"},
}
```

トークンベース認証の場合は、`headers` に `{"authorization": f"Bearer {token}"}` を使用してください。