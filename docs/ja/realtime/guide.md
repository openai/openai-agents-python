---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK のリアルタイム機能を使って音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイム エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

リアルタイム エージェントは、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを可能にします。OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声会話を実現し、割り込みにも優雅に対応します。

## アーキテクチャ

### コアコンポーネント

リアルタイム システムは、いくつかの重要なコンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs で構成されたエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常は ユーザー が会話を開始するたびに 1 つ作成し、会話が完了するまで維持します。
-   **RealtimeModel**: 基盤となるモデル インターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

典型的なリアルタイム セッションの流れは次のとおりです。

1. **RealtimeAgent を作成** し、instructions、tools、handoffs を設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと設定オプションを渡します。
3. **セッションを開始** します。`await runner.run()` を使用すると RealtimeSession が返ります。
4. **音声またはテキスト メッセージを送信** します。`send_audio()` または `send_message()` を使用します。
5. **イベントを監視** します。セッションを反復処理して、音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーなどのイベントを受信します。
6. **割り込みを処理** します。ユーザー がエージェントの発話にかぶせた場合、現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、リアルタイム モデルとの永続的な接続を管理します。

## エージェントの設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェント レベルではなく、セッション レベルで設定します。
-   structured outputs はサポートされません（`outputType` はサポートされません）。
-   ボイスはエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   その他の機能（ツール、ハンドオフ、instructions）は同様に動作します。

## セッションの設定

### モデル設定

セッション設定では、基盤となるリアルタイム モデルの動作を制御できます。モデル名（`gpt-realtime` など）、ボイスの選択（alloy、echo、fable、onyx、nova、shimmer）、対応するモダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声入力と出力をどのように扱うかを制御します。Whisper などのモデルを使用した入力音声の書き起こし、言語設定、ドメイン特有の用語の精度を高めるための書き起こしプロンプトを設定できます。ターン検出設定では、エージェントが応答を開始・停止すべきタイミングを制御し、音声活動検出しきい値、無音時間、検出された発話の前後に付けるパディングなどのオプションを提供します。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、リアルタイム エージェントは会話中に実行される 関数ツール をサポートします。

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

ハンドオフにより、専門化されたエージェント間で会話を移譲できます。

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

## イベント処理

セッションは、セッション オブジェクトを反復処理することで監視できるイベントをストリーミングします。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。主に対応すべきイベントは次のとおりです。

-   **audio**: エージェントの応答からの raw な音声データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザー がエージェントを割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイム エージェントでサポートされるのは出力 ガードレール のみです。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、変更可能です。

ガードレールは `RealtimeAgent` に直接付与するか、セッションの `run_config` から提供できます。両方のソースからのガードレールは併せて実行されます。

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

ガードレールが発火すると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を割り込むことがあります。デバウンスの動作は、安全性とリアルタイム性能要件のバランスを取るのに役立ちます。テキスト エージェントと異なり、リアルタイム エージェントはガードレールが発火しても **Exception** をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力に対しては、`audio` イベントを監視し、任意の音声ライブラリで音声データを再生してください。ユーザー がエージェントを割り込んだ際に即座に再生を停止し、キューにある音声をクリアするため、`audio_interrupted` イベントを必ず監視してください。

## SIP 連携

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 経由で着信する電話にリアルタイム エージェントを接続できます。SDK には [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] が用意されており、SIP 上でメディアをネゴシエートしつつ、同じエージェント フローを再利用します。

使用するには、モデル インスタンスを runner に渡し、セッション開始時に SIP の `call_id` を指定します。コール ID は、着信を知らせる webhook によって渡されます。

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

発信者が電話を切ると、SIP セッションは終了し、リアルタイム接続は自動的に閉じられます。完全なテレフォニーのサンプルについては、[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタム リスナーの追加や高度な操作を行うことができます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、高度なユースケース向けに接続をより低レベルで制御できる [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

動作する完全なサンプルは、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。