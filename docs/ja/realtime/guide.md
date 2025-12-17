---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、重大な変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話フローを実現し、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答します。 OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声対話と、割り込みを適切に処理する能力を提供します。

## アーキテクチャ

### コアコンポーネント

realtime システムは、いくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、ツール、ハンドオフで構成された エージェント です。
-   **RealtimeRunner**: 構成を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常、 ユーザー が会話を開始するたびに 1 つ作成し、会話が終わるまで維持します。
-   **RealtimeModel**: 基盤となるモデルのインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションは、次のフローに従います。

1. instructions、ツール、ハンドオフを使用して **RealtimeAgent を作成** します。
2. エージェントと構成オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使って **セッションを開始** します。これは RealtimeSession を返します。
4. `send_audio()` または `send_message()` を使用して **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには、音声出力、トランスクリプト、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザー がエージェントの発話に被せて話したときに **割り込みを処理** します。これにより現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続的な接続を管理します。

## エージェント構成

RealtimeAgent は、通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。 API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご確認ください。

通常のエージェントとの主な違い:

-   モデル選択はエージェント レベルではなく、セッション レベルで構成します。
-   structured outputs は非対応です（`outputType` はサポートされません）。
-   音声はエージェントごとに構成できますが、最初のエージェントが話し始めた後は変更できません。
-   ツール、ハンドオフ、instructions などのその他の機能は同様に機能します。

## セッション構成

### モデル設定

セッション構成では、基盤となる realtime モデルの動作を制御できます。モデル名（`gpt-realtime` など）、ボイス選択（alloy、echo、fable、onyx、nova、shimmer）、および対応するモダリティ（テキストおよび/または音声）を構成できます。音声フォーマットは入力と出力の両方に設定でき、既定では PCM16 です。

### 音声設定

音声設定は、セッションが音声入出力をどのように扱うかを制御します。 Whisper のようなモデルを使用した入力音声の文字起こし、言語設定、ドメイン特有の用語の精度を高めるための文字起こしプロンプトを構成できます。ターン検出設定では、エージェントがいつ応答を開始・停止すべきかを制御し、音声活動検出のしきい値、無音時間、検出された発話の前後のパディングなどを設定できます。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、realtime エージェントは会話中に実行される 関数ツール をサポートします。

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

## イベント処理

セッションは、セッションオブジェクトを反復処理することでリッスンできるイベントをストリーミングします。イベントには、音声出力チャンク、文字起こし結果、ツールの実行開始と終了、エージェントのハンドオフ、エラーが含まれます。主に対処すべきイベントは次のとおりです。

-   **audio**: エージェントの応答からの raw な音声データ
-   **audio_end**: エージェントが話し終えた
-   **audio_interrupted**: ユーザー がエージェントを中断した
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでは出力ガードレールのみがサポートされています。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、構成可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` 経由で提供できます。両方のソースからのガードレールは一緒に実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を中断できます。デバウンスの動作は、安全性とリアルタイム性能要件のバランスを取るのに役立ちます。テキスト エージェントと異なり、realtime エージェントはガードレールが作動しても例外をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで音声データを再生してください。ユーザー がエージェントを中断した場合にすぐに再生を停止し、キューにある音声をクリアできるよう、`audio_interrupted` イベントを必ずリッスンしてください。

## SIP 連携

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 経由で着信する電話に realtime エージェントを接続できます。 SDK は [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を提供しており、SIP 上でメディアをネゴシエートしながら同じエージェントフローを再利用します。

使用するには、ランナーにモデルインスタンスを渡し、セッション開始時に SIP の `call_id` を指定します。コール ID は、着信を知らせる Webhook によって届けられます。

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

発信者が電話を切ると、SIP セッションは終了し、realtime 接続は自動的に閉じられます。完全なテレフォニーの code examples は、[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## 直接モデルアクセス

基盤となるモデルにアクセスして、カスタムリスナーを追加したり、高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースに直接アクセスできます。

## 例

完全な動作する code examples は、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。