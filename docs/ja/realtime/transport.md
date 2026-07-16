---
search:
  exclude: true
---
# Realtime トランスポート

このページは、realtime エージェントを Python アプリケーションにどのように組み込むかを判断するために使用してください。

!!! note "Python SDK の境界"

    Python SDK には、ブラウザー WebRTC トランスポートは含まれて **いません**。このページは、Python SDK のトランスポート選択肢であるサーバー側 WebSocket と SIP アタッチフローのみを扱います。ブラウザー WebRTC は別のプラットフォームトピックであり、公式の [WebRTC による Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/) ガイドに記載されています。

## 判断ガイド

| 目的 | はじめに | 理由 |
| --- | --- | --- |
| サーバー管理の realtime アプリを構築する | [クイックスタート](quickstart.md) | デフォルトの Python パスは、`RealtimeRunner` によって管理されるサーバー側 WebSocket セッションです。 |
| 選択すべきトランスポートとデプロイ形態を理解する | このページ | トランスポートやデプロイ形態を決定する前に使用してください。 |
| エージェントを電話または SIP 通話にアタッチする | [Realtime ガイド](guide.md) と [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | このリポジトリには、`call_id` によって駆動される SIP アタッチフローが含まれています。 |

## デフォルトの Python パスであるサーバー側 WebSocket

カスタム `RealtimeModel` を渡さない限り、`RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用します。

つまり、標準的な Python トポロジーは次のようになります。

1. Python サービスが `RealtimeRunner` を作成します。
2. `await runner.run()` が `RealtimeSession` を返します。
3. セッションに入り、テキスト、構造化メッセージ、または音声を送信します。
4. `RealtimeSessionEvent` 項目を消費し、音声またはトランスクリプトをアプリケーションに転送します。

これは、コアデモアプリ、CLI の例、Twilio Media Streams の例で使用されているトポロジーです。

-   [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
-   [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
-   [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

サーバーが音声パイプライン、ツール実行、承認フロー、履歴処理を管理する場合は、このパスを使用してください。

## テレフォニー向けパスとしての SIP アタッチ

このリポジトリで説明されているテレフォニーフローでは、Python SDK は `call_id` を介して既存の realtime 通話にアタッチします。

このトポロジーは次のようになります。

1. OpenAI が `realtime.call.incoming` などの webhook をサービスに送信します。
2. サービスが Realtime Calls API を通じて通話を受け入れます。
3. Python サービスが `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())` を開始します。
4. セッションは `model_config={"call_id": ...}` で接続し、その後は他の realtime セッションと同様にイベントを処理します。

これは [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) に示されているトポロジーです。

より広範な Realtime API でも、一部のサーバー側制御パターンで `call_id` を使用しますが、このリポジトリに含まれるアタッチ例は SIP です。

## この SDK の範囲外であるブラウザー WebRTC

アプリの主なクライアントが Realtime WebRTC を使用するブラウザーである場合:

-   このリポジトリの Python SDK ドキュメントの範囲外として扱ってください。
-   クライアント側のフローとイベントモデルについては、公式の [WebRTC による Realtime API](https://developers.openai.com/api/docs/guides/realtime-webrtc/) および [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations/) ドキュメントを使用してください。
-   ブラウザー WebRTC クライアントの上にサイドバンドのサーバー接続が必要な場合は、公式の [Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls/) ガイドを使用してください。
-   このリポジトリが、ブラウザー側の `RTCPeerConnection` 抽象化や、すぐに使えるブラウザー WebRTC サンプルを提供することは期待しないでください。

このリポジトリには、現在、ブラウザー WebRTC と Python サイドバンドを組み合わせた例も含まれていません。

## カスタムエンドポイントとアタッチポイント

[`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig] のトランスポート設定サーフェスを使用すると、デフォルトのパスを調整できます。

-   `url`: WebSocket エンドポイントを上書きします
-   `headers`: Azure 認証ヘッダーなどの明示的なヘッダーを指定します
-   `api_key`: API キーを直接、またはコールバック経由で渡します
-   `call_id`: 既存の realtime 通話にアタッチします。このリポジトリで記載されている例は SIP です。
-   `playback_tracker`: 割り込み処理のために実際の再生進捗を報告します

トポロジーを選択した後の詳細なライフサイクルと機能サーフェスについては、[Realtime エージェントガイド](guide.md) を参照してください。