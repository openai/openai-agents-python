---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を使用して音声対応 AI エージェントを構築する方法を詳しく説明します。

!!! warning "Beta 機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が入る可能性があります。

## 概要

Realtime エージェントは会話フローをリアルタイムで処理し、音声やテキスト入力を受け取って即時に音声で応答します。 OpenAI の Realtime API と永続接続を維持することで、低レイテンシかつ自然な音声対話を実現し、発話の割り込みにも柔軟に対応できます。

## アーキテクチャ

### コアコンポーネント

- **RealtimeAgent**: instructions、 tools、 handoffs で構成されたエージェント。  
- **RealtimeRunner**: 設定を管理します。 `runner.run()` を呼び出してセッションを取得できます。  
- **RealtimeSession**: 1 回の対話セッション。ユーザーが会話を開始するたびに作成し、会話が終了するまで維持します。  
- **RealtimeModel**: 基盤となるモデルインターフェース (一般的には OpenAI の WebSocket 実装)  

### セッションフロー

典型的な Realtime セッションは次の流れで進みます。

1. instructions、 tools、 handoffs を指定して **RealtimeAgent** を作成します。  
2. **RealtimeRunner** をセットアップし、エージェントと各種設定を渡します。  
3. `await runner.run()` で **セッションを開始** し、 RealtimeSession を取得します。  
4. `send_audio()` または `send_message()` で **音声またはテキストメッセージを送信** します。  
5. セッションをイテレートして **イベントを監視** します。イベントには音声出力、文字起こし、ツール呼び出し、 handoffs、エラーが含まれます。  
6. ユーザーが発話を割り込んだ場合には **割り込みを処理** し、現在の音声生成を自動停止します。  

セッションは会話履歴を保持し、 Realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスとほぼ同じですが、いくつか重要な違いがあります。詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API リファレンスをご覧ください。

主な違い:

- モデル選択はエージェントではなくセッションレベルで設定します。  
- structured outputs (`outputType`) はサポートされません。  
- 音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。  
- tools、 handoffs、 instructions などその他の機能は同じように動作します。  

## セッション設定

### モデル設定

セッション設定では基盤となる Realtime モデルの挙動を制御できます。モデル名 (たとえば `gpt-4o-realtime-preview`) や音声 (alloy、 echo、 fable、 onyx、 nova、 shimmer) の選択、対応モダリティ (テキスト / 音声) を指定できます。入力・出力の音声フォーマットは PCM16 がデフォルトです。

### オーディオ設定

オーディオ設定では音声入力と出力の取り扱いを制御します。 Whisper などのモデルを用いた入力音声の文字起こし、言語設定、ドメイン特有の用語精度を高める transcription prompt を指定できます。ターン検出設定では、音声活動検知のしきい値、無音時間、検知した音声前後のパディングなどを設定し、エージェントがいつ応答を開始・終了すべきかを制御します。

## Tools と Functions

### Tools の追加

通常のエージェントと同様に、 Realtime エージェントも会話中に実行される function tools をサポートします。

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

## Handoffs

### Handoffs の作成

Handoffs を使用すると、会話を専門エージェント間で引き継げます。

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

セッションはイベントをストリーミングし、セッションオブジェクトをイテレートすることで取得できます。イベントには音声出力チャンク、文字起こし結果、ツール実行開始 / 終了、エージェント handoffs、エラーなどがあります。主なイベントは次のとおりです。

- **audio**: エージェントの応答からの raw 音声データ  
- **audio_end**: エージェントの発話が終了  
- **audio_interrupted**: ユーザーがエージェントの発話を割り込み  
- **tool_start/tool_end**: ツール実行ライフサイクル  
- **handoff**: エージェントの handoff が発生  
- **error**: 処理中にエラーが発生  

詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] をご参照ください。

## ガードレール

Realtime エージェントでは出力ガードレールのみサポートされます。パフォーマンスを保つためにデバウンス処理が施されており、毎単語ではなく定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、変更可能です。

ガードレールが発火すると `guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込むことがあります。テキストエージェントと異なり、 Realtime エージェントではガードレール発火時に Exception はスローされません。

## オーディオ処理

音声を送信する場合は [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を、テキストを送信する場合は [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用します。

音声出力を受信するには `audio` イベントを監視し、好みのオーディオライブラリで再生してください。ユーザーが発話を割り込んだ際は `audio_interrupted` イベントを監視して即座に再生を停止し、キューにある音声をクリアするようにしてください。

## 直接モデルへアクセス

より低レベルの制御が必要な場合や独自のリスナーを追加したい場合は、基盤モデルに直接アクセスできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、高度なユースケース向けに [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作例は [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。 UI あり / なしのデモが含まれています。