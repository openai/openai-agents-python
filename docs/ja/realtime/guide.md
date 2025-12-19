---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK のリアルタイム機能を用いて、音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "Beta feature"
リアルタイム エージェントはベータ版です。実装の改善に伴い、非互換の変更が発生する可能性があります。

## 概要

リアルタイム エージェントは、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを可能にします。 OpenAI の Realtime API と永続的に接続し、低遅延で自然な音声会話や割り込みへの優雅な対応を実現します。

## アーキテクチャ

### コアコンポーネント

リアルタイム システムは、次の主要コンポーネントで構成されます。

- **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェント。
- **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
- **RealtimeSession**: 単一の対話セッション。通常、 ユーザー が会話を開始するたびに 1 つ作成し、会話が終了するまで維持します。
- **RealtimeModel**: 基盤となるモデルのインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的なリアルタイム セッションは、次のフローに従います。

1. **RealtimeAgent を作成する**: instructions、tools、ハンドオフを設定します。
2. **RealtimeRunner をセットアップする**: エージェントと構成オプションを指定します。
3. **セッションを開始する**: `await runner.run()` を使用して RealtimeSession を取得します。
4. **音声またはテキスト メッセージを送信する**: `send_audio()` または `send_message()` を使用します。
5. **イベントをリッスンする**: セッションを反復処理してイベントを受け取ります。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. **割り込みを処理する**: ユーザー がエージェントに被せて話した場合、現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、リアルタイム モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。 API の詳細については、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスを参照してください。

通常のエージェントとの主な相違点:

- モデルの選択はエージェント レベルではなく、セッション レベルで構成します。
- structured outputs はサポートされません（`outputType` は未対応）。
- 音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
- その他の機能（tools、ハンドオフ、instructions）は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となるリアルタイム モデルの動作を制御できます。モデル名（`gpt-realtime` など）、音声（alloy、echo、fable、onyx、nova、shimmer）の選択、対応するモダリティ（テキストおよび/または音声）を構成できます。音声フォーマットは入出力の両方に設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声の入出力をどのように処理するかを制御します。 Whisper などのモデルを使用した入力音声の文字起こし、言語設定、ドメイン固有用語の精度を高めるための文字起こしプロンプトを構成できます。応答開始/終了の検出（ターン検出）は、音声活動検出のしきい値、無音時間、検出音声の前後パディングなどのオプションで調整できます。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、リアルタイム エージェントは会話中に実行される 関数ツール をサポートします:

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

ハンドオフにより、専門化されたエージェント間で会話を引き継ぐことができます。

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

セッションは、セッション オブジェクトを反復処理してリッスンできるイベントをストリーミングします。イベントには、音声出力チャンク、文字起こし結果、ツールの実行開始/終了、エージェントのハンドオフ、エラーが含まれます。特に処理すべき主なイベントは次のとおりです。

- **audio**: エージェントの応答からの raw 音声データ
- **audio_end**: エージェントの発話が終了
- **audio_interrupted**: ユーザー によるエージェントの割り込み
- **tool_start/tool_end**: ツール実行のライフサイクル
- **handoff**: エージェントのハンドオフが発生
- **error**: 処理中にエラーが発生

完全なイベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイム エージェントでサポートされるのは出力 ガードレール のみです。これらの ガードレール はデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、構成可能です。

ガードレール は `RealtimeAgent` に直接アタッチすることも、セッションの `run_config` を通じて提供することもできます。両方のソースからの ガードレール は併用されます。

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

ガードレール がトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンスの動作により、安全性とリアルタイム性能要件のバランスが取られます。テキスト エージェントと異なり、リアルタイム エージェントは ガードレール に引っかかっても例外をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストをセッションに送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで再生してください。ユーザー がエージェントを割り込んだ際に即座に再生を停止し、キュー済み音声をクリアするため、`audio_interrupted` イベントも必ずリッスンしてください。

## SIP 連携

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 経由で着信する電話にリアルタイム エージェントを接続できます。 SDK は [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を提供しており、SIP 上でメディアをネゴシエートしつつ、同じエージェント フローを再利用します。

使用するには、モデル インスタンスを Runner に渡し、セッション開始時に SIP の `call_id` を指定します。コール ID は、着信を通知する Webhook で渡されます。

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

発信者が電話を切ると SIP セッションは終了し、リアルタイム接続は自動的に閉じられます。完全なテレフォニーの例については、[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタム リスナーを追加したり高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケースに対して、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全に動作する例は、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。