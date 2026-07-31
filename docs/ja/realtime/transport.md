---
search:
  exclude: true
---
# リアルタイムトランスポート

リアルタイムエージェントを Python アプリケーションにどのように組み込むかを判断するには、このページを使用してください。

!!! note "Python SDK の対象範囲"

    Python SDK には、ブラウザー向け WebRTC トランスポートは **含まれていません** 。このページでは、Python SDK におけるトランスポートの選択肢である、サーバーサイド WebSocket と SIP アタッチフローのみを扱います。ブラウザー WebRTC は別のプラットフォームトピックです。詳細については、公式の [WebRTC を使用した Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/) ガイドを参照してください。

## 判断ガイド

| 目的 | 最初に参照するもの | 理由 |
| --- | --- | --- |
| サーバー管理型のリアルタイムアプリを構築する | [クイックスタート](quickstart.md) | Python のデフォルト経路は、`RealtimeRunner` が管理するサーバーサイド WebSocket セッションです。 |
| 選択すべきトランスポートとデプロイ形態を理解する | このページ | トランスポートまたはデプロイ形態を決定する前に、このページを参照してください。 |
| エージェントを電話または SIP 通話にアタッチする | [リアルタイムガイド](guide.md)および [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | このリポジトリには、`call_id` によって駆動される SIP アタッチフローが含まれています。 |

## Python のデフォルト経路としてのサーバーサイド WebSocket

カスタムの `RealtimeModel` を渡さない限り、`RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用します。

つまり、標準的な Python トポロジーは次のようになります。

1. Python サービスが `RealtimeRunner` を作成します。
2. `await runner.run()` が `RealtimeSession` を返します。
3. セッションに入り、テキスト、構造化メッセージ、または音声を送信します。
4. `RealtimeSessionEvent` の項目を処理し、音声または文字起こしをアプリケーションに転送します。

このトポロジーは、コアデモアプリ、CLI のコード例、および Twilio Media Streams のコード例で使用されています。

-   [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
-   [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
-   [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

サーバーが音声パイプライン、ツール実行、承認フロー、および履歴処理を管理する場合は、この経路を使用してください。

### 低レベル WebSocket チューニング

基盤となるサーバーサイド WebSocket 接続を調整する必要がある場合は、`transport_config` を `OpenAIRealtimeWebSocketModel` に渡します。

```python
from agents.realtime import (
    OpenAIRealtimeWebSocketModel,
    RealtimeAgent,
    RealtimeRunner,
)

agent = RealtimeAgent(name="Assistant")
model = OpenAIRealtimeWebSocketModel(
    transport_config={
        "ping_interval": 20.0,
        "ping_timeout": 60.0,
        "handshake_timeout": 30.0,
        "max_size": 8 * 1024 * 1024,
    }
)
runner = RealtimeRunner(starting_agent=agent, model=model)
```

サポートされるオプションは次のとおりです。

-   `ping_interval`: クライアントのキープアライブ ping の間隔（秒）。ping を無効にするには `None` を設定します。
-   `ping_timeout`: 切断するまで pong を待機する秒数。ハートビートタイムアウトを発生させずに pong の遅延を許容するには、`None` を設定します。
-   `handshake_timeout`: 最初の接続ハンドシェイクを待機する秒数。
-   `max_size`: 受信する WebSocket メッセージの最大サイズ（バイト単位）。SDK のデフォルトは `None` で、受信メッセージのサイズは無制限になります。メッセージ単位のメモリ使用量を制限する必要がある場合は、明示的な上限を設定します。

これらの設定は、Realtime API セッションではなく、クライアント接続を構成します。エンドポイント、認証、通話のアタッチ、および再生設定には、引き続き `RealtimeModelConfig` を使用してください。

## テレフォニー経路としての SIP アタッチ

このリポジトリに記載されているテレフォニーフローでは、Python SDK は `call_id` を介して既存のリアルタイム通話にアタッチします。

このトポロジーは次のようになります。

1. OpenAI が `realtime.call.incoming` などの Webhook をサービスに送信します。
2. サービスが Realtime Calls API を介して通話を受け入れます。
3. Python サービスが `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())` を開始します。
4. セッションが `model_config={"call_id": ...}` を使用して接続し、その後は他のリアルタイムセッションと同様にイベントを処理します。

これは、[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) に示されているトポロジーです。

Realtime API 全般でも、一部のサーバーサイド制御パターンで `call_id` が使用されますが、このリポジトリに含まれるアタッチのコード例は SIP です。

## Python SDK 対象外のブラウザー WebRTC

アプリの主要なクライアントが Realtime WebRTC を使用するブラウザーである場合は、次の点に注意してください。

-   このリポジトリの Python SDK ドキュメントの対象外として扱ってください。
-   クライアント側のフローとイベントモデルについては、公式の [WebRTC を使用した Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/)および [リアルタイム会話](https://developers.openai.com/api/docs/guides/realtime-conversations/)のドキュメントを参照してください。
-   ブラウザー WebRTC クライアントに加えてサイドバンドサーバー接続が必要な場合は、公式の [リアルタイムサーバーサイド制御](https://developers.openai.com/api/docs/guides/realtime-server-controls/)ガイドを参照してください。
-   このリポジトリでは、ブラウザー側の `RTCPeerConnection` 抽象化や、すぐに使用できるブラウザー WebRTC のコード例は提供されません。

また、このリポジトリには現在、ブラウザー WebRTC と Python サイドバンドを組み合わせたコード例も含まれていません。

## カスタムエンドポイントとアタッチポイント

[`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig] のトランスポート設定項目を使用すると、デフォルトの経路を調整できます。

-   `url`: WebSocket エンドポイントを上書き
-   `headers`: Azure 認証ヘッダーなどの明示的なヘッダーを指定
-   `api_key`: API キーを直接、またはコールバックを介して渡す
-   `call_id`: 既存のリアルタイム通話にアタッチ。このリポジトリに記載されているコード例は SIP です。
-   `playback_tracker`: 割り込み処理のために実際の再生進捗を報告

トポロジーを選択した後の詳細なライフサイクルと機能範囲については、[リアルタイムエージェントガイド](guide.md)を参照してください。