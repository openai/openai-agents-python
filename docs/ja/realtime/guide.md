---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK のリアルタイム機能を用いて、音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイム エージェントはベータ版です。実装の改良に伴い、互換性のない変更が発生する可能性があります。

## 概要

リアルタイム エージェントは、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを可能にします。 OpenAI の Realtime API と永続的な接続を維持し、低遅延で自然な音声会話や割り込みの優雅な処理を実現します。

## アーキテクチャ

### 中核コンポーネント

リアルタイム システムはいくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs で構成された エージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常、 ユーザー が会話を開始するたびに 1 つ作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデル インターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的なリアルタイム セッションの流れは次のとおりです。

1. instructions、tools、handoffs を用いて **RealtimeAgent を作成** します。
2. エージェントと設定オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使用して **セッションを開始** します。これは RealtimeSession を返します。
4. `send_audio()` または `send_message()` を使って **音声またはテキスト メッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザー がエージェントの発話に重ねて話す **割り込みを処理** します。これにより現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、リアルタイム モデルとの永続的接続を管理します。

## エージェント構成

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつかの重要な違いがあります。完全な API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご参照ください。

通常の エージェント との主な違い:

-   モデルの選択はエージェント レベルではなくセッション レベルで設定します。
-   structured outputs は非対応です（`outputType` は未対応）。
-   音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   その他の機能（tools、handoffs、instructions）は同様に動作します。

## セッション構成

### モデル設定

セッション構成では、基盤となるリアルタイム モデルの動作を制御できます。モデル名（`gpt-realtime` など）、音声の選択（alloy、echo、fable、onyx、nova、shimmer）、および対応するモダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方に設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定は、セッションが音声の入出力をどのように扱うかを制御します。Whisper などのモデルを使った入力音声の文字起こし、言語設定、ドメイン固有用語の精度を高めるための文字起こしプロンプトを設定できます。ターン検出では、エージェントがいつ応答を開始・終了すべきかを制御し、音声活動検出のしきい値、無音時間、検出された発話の前後パディングなどを設定できます。

## ツールと関数

### ツールの追加

通常の エージェント と同様に、リアルタイム エージェントは会話中に実行される 関数ツール をサポートします。

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

ハンドオフにより、専門化された エージェント 間で会話を転送できます。

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

セッションは、セッション オブジェクトを反復処理することでリッスンできるイベントをストリーミングします。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始・終了、エージェントのハンドオフ、エラーが含まれます。特に処理すべき主なイベントは以下です。

-   **audio**: エージェントの応答からの Raw 音声データ
-   **audio_end**: エージェントの発話が完了
-   **audio_interrupted**: ユーザー によるエージェントの割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

完全なイベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイム エージェントでサポートされるのは出力 ガードレール のみです。これらの ガードレール はデバウンスされ、リアルタイム生成中の性能問題を避けるために（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレール は `RealtimeAgent` に直接アタッチするか、セッションの `run_config` 経由で提供できます。両方のソースからの ガードレール は一緒に実行されます。

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

ガードレール がトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を中断できます。デバウンスの挙動により、安全性とリアルタイム性能要件のバランスが取られます。テキスト エージェント と異なり、リアルタイム エージェントはガードレール が作動しても Exception をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力については、`audio` イベントをリッスンして、選択した音声ライブラリで音声データを再生してください。ユーザー がエージェントを割り込んだ際に即座に再生を停止し、キューにある音声をクリアするため、`audio_interrupted` イベントも必ずリッスンしてください。

## SIP 連携

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 経由で着信する電話にリアルタイム エージェントをアタッチできます。SDK は、SIP 上でメディアをネゴシエートしつつ同じエージェント フローを再利用する [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を提供します。

使用するには、モデル インスタンスを runner に渡し、セッション開始時に SIP の `call_id` を指定します。コール ID は着信を通知する webhook により配信されます。

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

発信者が切断すると SIP セッションは終了し、リアルタイム接続は自動的にクローズされます。完全なテレフォニーのコード例は、[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタム リスナーを追加したり、高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルで制御する必要がある高度なユースケースに向けて、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全に動作するコード例は、[examples/realtime directory](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI コンポーネントの有無それぞれのデモが含まれています。