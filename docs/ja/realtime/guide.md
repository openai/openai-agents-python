---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を使用して音声対応の AI エージェントを構築する方法を詳しく解説します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装を改善する過程で互換性が失われる変更が入る可能性があります。

## 概要

Realtime エージェントは、音声およびテキスト入力をリアルタイムで処理し、音声で応答する会話フローを実現します。 OpenAI の Realtime API と永続的に接続を維持することで、低レイテンシで自然な音声対話が可能となり、ユーザーの割り込みにもスムーズに対応できます。

## アーキテクチャ

### 主要コンポーネント

Realtime システムは次の主要コンポーネントで構成されています。

- **RealtimeAgent**: instructions、tools、handoffs を設定したエージェントです。  
- **RealtimeRunner**: 設定を管理します。 `runner.run()` を呼び出すことでセッションを取得できます。  
- **RealtimeSession**: 1 回の対話セッションを表します。通常、ユーザーが会話を開始するたびに作成し、会話が終了するまで保持します。  
- **RealtimeModel**: 基盤となるモデル インターフェース（通常は OpenAI の WebSocket 実装）です。

### セッションフロー

一般的な Realtime セッションは次の流れになります。

1. **RealtimeAgent** を作成し、instructions、tools、handoffs を設定します。  
2. エージェントと設定オプションを指定して **RealtimeRunner** を準備します。  
3. `await runner.run()` を実行して **セッションを開始** します。これにより RealtimeSession が返されます。  
4. `send_audio()` または `send_message()` を使用して **音声またはテキスト メッセージを送信** します。  
5. セッションを反復処理して **イベントを受信** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなどが含まれます。  
6. ユーザーがエージェントの発話に重ねて話した場合の **割り込み処理** を行います。割り込み時には自動的に現在の音声生成が停止します。  

セッションは会話履歴を保持し、Realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスとほぼ同じですが、いくつか重要な違いがあります。詳細な API は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] を参照してください。

主な違い

- モデルの選択はエージェント レベルではなくセッション レベルで設定します。  
- structured outputs（ `outputType` ）はサポートされません。  
- ボイスはエージェントごとに設定できますが、最初のエージェントが発話した後に変更することはできません。  
- tools、handoffs、instructions などその他の機能は通常のエージェントと同様に動作します。  

## セッション設定

### モデル設定

セッション設定では基盤となる Realtime モデルの動作を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、ボイス（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキスト／音声）を指定できます。音声の入出力形式も設定可能で、デフォルトは PCM16 です。

### オーディオ設定

オーディオ設定では音声入力および出力の扱いを制御します。Whisper などのモデルを用いた入力音声の文字起こし、言語設定、ドメイン固有語の認識精度を向上させる transcription prompts を指定できます。ターン検出では、音声活動検出のしきい値、無音時間、検出した音声の前後パディングなどを設定し、エージェントが応答を開始・終了するタイミングを制御します。

## Tools と Functions

### Tools の追加

通常のエージェントと同様に、Realtime エージェントは会話中に実行される function tools をサポートします。

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

ハンドオフを使用すると、会話を専門化されたエージェント間で転送できます。

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

セッションはイベントをストリーム配信するため、セッション オブジェクトを反復処理してイベントを受信できます。主なイベントは次のとおりです。

- **audio**: エージェントの応答から生成される raw 音声データ  
- **audio_end**: エージェントの発話が終了したことを示します  
- **audio_interrupted**: ユーザーがエージェントの発話を割り込んだことを示します  
- **tool_start/tool_end**: ツール実行のライフサイクル  
- **handoff**: エージェントのハンドオフが発生したことを示します  
- **error**: 処理中にエラーが発生しました  

すべてのイベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでは出力ガードレールのみがサポートされています。パフォーマンスへの影響を避けるため、ガードレールはデバウンスされて定期的に（毎単語ではなく）実行されます。デフォルトのデバウンス長は 100 文字ですが、変更可能です。

ガードレールがトリップすると `guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンスにより安全性とリアルタイム性能のバランスを取っています。テキスト エージェントと異なり、Realtime エージェントではガードレール トリップ時に Exception は送出されません。

## オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声を、 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストをセッションに送信します。

音声出力を受け取る際は `audio` イベントを監視し、任意のオーディオ ライブラリで再生してください。ユーザーが割り込んだ場合は `audio_interrupted` イベントを検出して即座に再生を停止し、キューに残っている音声をクリアする必要があります。

## 直接モデルアクセス

より高度な操作やカスタム リスナーを追加するために、基盤となるモデルへ直接アクセスできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、高度なユースケースで接続を低レベルで制御するための [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースに直接アクセスできます。

## 例

実際に動作するサンプルは、 UI あり / なし のデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。