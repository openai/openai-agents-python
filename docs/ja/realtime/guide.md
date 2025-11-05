---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いた音声対応 AI エージェントの構築について詳しく説明します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話フローを可能にし、音声およびテキスト入力をリアルタイムで処理し、リアルタイム音声でレスポンスします。OpenAI の Realtime API との永続接続を維持し、低レイテンシで自然な音声会話や中断へのスムーズな対応を実現します。

## アーキテクチャ

### 中核コンポーネント

realtime システムは次の主要コンポーネントで構成されます:

-   **RealtimeAgent**: instructions、tools、handoffs を設定したエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出すとセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常は ユーザー が会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

典型的な realtime セッションは次の流れに従います:

1. **RealtimeAgent を作成** し、instructions、tools、handoffs を設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと設定オプションを指定します。
3. `await runner.run()` を使用して **セッションを開始** します。戻り値は RealtimeSession です。
4. `send_audio()` または `send_message()` を使用して **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントを監視** します。イベントには音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーなどが含まれます。
6. ユーザー がエージェントにかぶせて話す **割り込みを処理** します。現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご参照ください。

通常のエージェントとの差異:

-   モデルの選択はエージェントレベルではなくセッションレベルで設定します。
-   structured outputs はサポートされません（`outputType` は非対応）。
-   音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   その他の機能（tools、handoffs、instructions）は同様に動作します。

## セッション設定

### モデル設定

セッション設定により、基盤となる realtime モデルの挙動を制御できます。モデル名（`gpt-realtime` など）、音声選択（alloy、echo、fable、onyx、nova、shimmer）、サポートするモダリティ（テキストおよび/または音声）を構成できます。音声フォーマットは入力と出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声入力と出力をどのように扱うかを制御します。Whisper などのモデルを用いた入力音声の書き起こし、言語設定、専門用語の精度向上のための書き起こしプロンプトを設定できます。ターン検出設定では、音声活動検出のしきい値、無音時間、検出音声周りのパディングなど、エージェントが応答を開始・停止すべきタイミングを制御します。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、realtime エージェントは会話中に実行される 関数ツール をサポートします:

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

ハンドオフ により、会話を専門特化したエージェント間で引き継げます。

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

セッションは、セッションオブジェクトを反復処理することで監視できるイベントをストリーミングします。イベントには音声出力チャンク、書き起こし結果、ツール実行の開始/終了、エージェントのハンドオフ、エラーが含まれます。特に処理すべき主要イベントは次のとおりです:

-   **audio**: エージェントのレスポンスからの raw 音声データ
-   **audio_end**: エージェントの発話完了
-   **audio_interrupted**: ユーザー による割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフ発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでサポートされるのは出力 ガードレール のみです。リアルタイム生成時のパフォーマンス問題を避けるため、これらはデバウンスされ、（単語ごとではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレール は `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースの ガードレール は併せて実行されます。

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

ガードレール がトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在のレスポンスを中断できます。デバウンス動作は、安全性とリアルタイム性能要件のバランスを取るのに役立ちます。テキストエージェントと異なり、realtime エージェントは ガードレール が作動しても例外を **スローしません**。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストをセッションに送信します。

音声出力については、`audio` イベントを監視し、任意の音声ライブラリで音声データを再生してください。ユーザー がエージェントを中断した際に即時に再生を停止し、キュー済みの音声をクリアするため、`audio_interrupted` イベントを必ず監視してください。

## SIP 連携

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) を介して着信する電話に realtime エージェントを接続できます。SDK は [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を提供しており、SIP 上でメディアをネゴシエートしつつ、同じエージェントフローを再利用します。

使用するには、モデルインスタンスを runner に渡し、セッション開始時に SIP の `call_id` を指定します。コール ID は、着信を通知する Webhook から配信されます。

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

発信者が電話を切ると、SIP セッションは終了し、realtime 接続は自動的に閉じられます。電話連携の完全な code examples は [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## 直接モデルアクセス

基盤となるモデルへアクセスし、カスタムリスナーの追加や高度な操作を実行できます:

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

動作する完全な code examples は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI コンポーネントの有無それぞれのデモを含みます。