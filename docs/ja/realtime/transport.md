---
search:
  exclude: true
---
# リアルタイムトランスポート

リアルタイムエージェントを Python アプリケーションにどのように組み込むかを判断する際は、このページを参照してください。

!!! note "Python SDK の境界"

    Python SDK には、ブラウザー向け WebRTC トランスポートは **含まれていません** 。このページでは、Python SDK のトランスポートの選択肢である、サーバー側 WebSocket と SIP 接続フローのみを扱います。ブラウザー WebRTC は別のプラットフォームトピックであり、公式の [WebRTC を使用する Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/) ガイドに記載されています。

## 選択ガイド {#decision-guide}

| 目的 | 最初に参照するもの | 理由 |
| --- | --- | --- |
| サーバー管理型のリアルタイムアプリを構築する | [クイックスタート](quickstart.md) | Python のデフォルトパスは、`RealtimeRunner` によって管理されるサーバー側 WebSocket セッションです。 |
| 選択すべきトランスポートとデプロイ構成を理解する | このページ | トランスポートまたはデプロイ構成を決定する前に、このページを参照してください。 |
| エージェントを電話または SIP 通話に接続する | [リアルタイムガイド](guide.md)および [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | このリポジトリには、`call_id` によって駆動される SIP 接続フローが含まれています。 |

## Python のデフォルトパスとなるサーバー側 WebSocket {#server-side-websocket-is-the-default-python-path}

カスタムの `RealtimeModel` を渡さない限り、`RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用します。

したがって、標準的な Python トポロジーは次のようになります。

1. Python サービスが `RealtimeRunner` を作成します。
2. `await runner.run()` が `RealtimeSession` を返します。
3. `RealtimeSession` を非同期コンテキストマネージャーとして開始し、テキスト、構造化メッセージ、または音声を送信します。
4. `RealtimeSessionEvent` の項目を処理し、音声または文字起こしをアプリケーションに転送します。

これは、コアデモアプリ、CLI のコード例、および Twilio Media Streams のコード例で使用されているトポロジーです。

-   [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
-   [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
-   [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

サーバーが音声パイプライン、ツール実行、承認フロー、および履歴処理を担う場合は、このパスを使用してください。

### 低レベル WebSocket の調整 {#low-level-websocket-tuning}

基盤となるサーバー側 WebSocket 接続を調整する必要がある場合は、`OpenAIRealtimeWebSocketModel` に `transport_config` を渡します。

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

サポートされているオプションは次のとおりです。

-   `ping_interval`: クライアントのキープアライブ ping 間隔（秒）です。ping を無効にするには、`None` に設定します。
-   `ping_timeout`: 切断するまで pong を待機する秒数です。ハートビートのタイムアウトを発生させずに pong の遅延を許容するには、`None` に設定します。
-   `handshake_timeout`: 最初の接続ハンドシェイクを待機する秒数です。
-   `max_size`: 受信 WebSocket メッセージの最大サイズ（バイト）です。SDK のデフォルトは `None` で、受信メッセージのサイズは無制限になります。メッセージごとのメモリ使用量を制限する必要がある場合は、明示的な上限を設定してください。

これらの設定は Realtime APIセッションではなく、クライアント接続を構成します。エンドポイント、認証、通話への接続、および再生設定には、引き続き `RealtimeModelConfig` を使用してください。

## 電話通信向けの SIP 接続 {#sip-attach-is-the-telephony-path}

このリポジトリに記載されている電話通信フローでは、Python SDK は `call_id` を介して既存のリアルタイム通話に接続します。

このトポロジーは次のようになります。

1. OpenAIが `realtime.call.incoming` などの Webhook をサービスに送信します。
2. サービスが Realtime Calls API を介して通話を受け付けます。
3. Python サービスが `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())` を開始します。
4. セッションが `model_config={"call_id": ...}` を使用して接続し、その後は他のリアルタイムセッションと同様にイベントを処理します。

これは、[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) に示されているトポロジーです。

より広範な Realtime APIでは、一部のサーバー側制御パターンに `call_id` も使用しますが、このリポジトリに含まれる接続のコード例では SIP を使用しています。

## SDK の対象外となるブラウザー WebRTC {#browser-webrtc-is-outside-this-sdk}

アプリの主要クライアントが Realtime WebRTC を使用するブラウザーである場合は、次の点に注意してください。

-   このリポジトリの Python SDK ドキュメントの対象外として扱ってください。
-   クライアント側のフローとイベントモデルについては、公式の [WebRTC を使用する Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/)および[リアルタイム会話](https://developers.openai.com/api/docs/guides/realtime-conversations/)のドキュメントを参照してください。
-   ブラウザー WebRTC クライアントに加えてサイドバンドサーバー接続が必要な場合は、公式の [Realtime のサーバー側制御](https://developers.openai.com/api/docs/guides/realtime-server-controls/)ガイドを参照してください。
-   このリポジトリでは、ブラウザー側の `RTCPeerConnection` 抽象化や、すぐに利用できるブラウザー WebRTC のコード例は提供されていません。

また、このリポジトリには現在、ブラウザー WebRTC と Python サイドバンドを組み合わせたコード例も含まれていません。

## カスタムエンドポイントと接続ポイント {#custom-endpoints-and-attach-points}

[`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig] のトランスポート設定インターフェースを使用すると、デフォルトのトランスポート動作をカスタマイズできます。

-   `url`: WebSocket エンドポイントを上書きします
-   `headers`: Azure 認証ヘッダーなどの明示的なヘッダーを指定します
-   `api_key`: API キーを直接、またはコールバック経由で渡します
-   `call_id`: 既存のリアルタイム通話に接続します。このリポジトリに記載されているコード例では SIP を使用します。
-   `playback_tracker`: 割り込み処理のために実際の再生進行状況を報告します

トポロジーを選択した後の詳細なライフサイクルと機能範囲については、[リアルタイムエージェントガイド](guide.md)を参照してください。