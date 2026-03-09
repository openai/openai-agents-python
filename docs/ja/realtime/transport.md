---
search:
  exclude: true
---
# Realtime トランスポート

このページは、realtime エージェントを Python アプリケーションにどのように組み込むかを判断するために使用します。

!!! note "Python SDK の境界"

    Python SDK にはブラウザー WebRTC トランスポートは **含まれていません** 。このページは Python SDK のトランスポート選択、つまりサーバーサイド WebSocket と SIP アタッチフローのみを対象としています。ブラウザー WebRTC は別のプラットフォームトピックであり、公式の [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/) ガイドに記載されています。

## 判断ガイド

| Goal | Start with | Why |
| --- | --- | --- |
| サーバー管理の realtime アプリを構築する | [Quickstart](quickstart.md) | デフォルトの Python パスは、`RealtimeRunner` で管理されるサーバーサイド WebSocket セッションです。 |
| どのトランスポートとデプロイ形状を選ぶべきか理解する | このページ | トランスポートやデプロイ形状を確定する前に、このページを使用してください。 |
| エージェントを電話または SIP 通話にアタッチする | [Realtime guide](guide.md) と [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | このリポジトリには、`call_id` で駆動する SIP アタッチフローが含まれています。 |

## サーバーサイド WebSocket というデフォルトの Python パス

`RealtimeRunner` は、カスタム `RealtimeModel` を渡さない限り `OpenAIRealtimeWebSocketModel` を使用します。

つまり、標準的な Python トポロジーは次のようになります。

1. Python サービスが `RealtimeRunner` を作成します。
2. `await runner.run()` は `RealtimeSession` を返します。
3. セッションに入り、テキスト、構造化メッセージ、または音声を送信します。
4. `RealtimeSessionEvent` 項目を消費し、音声またはトランスクリプトをアプリケーションに転送します。

このトポロジーは、コアデモアプリ、CLI 例、Twilio Media Streams 例で使用されています。

-   [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
-   [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
-   [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

サーバーが音声パイプライン、ツール実行、承認フロー、履歴処理を管理する場合は、このパスを使用してください。

## SIP アタッチというテレフォニーパス

このリポジトリで文書化されているテレフォニーフローでは、Python SDK は `call_id` を介して既存の realtime 通話にアタッチします。

このトポロジーは次のようになります。

1. OpenAI が `realtime.call.incoming` などの webhook をサービスに送信します。
2. サービスが Realtime Calls API を通じて通話を受け付けます。
3. Python サービスが `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())` を開始します。
4. セッションは `model_config={"call_id": ...}` で接続し、その後は他の realtime セッションと同様にイベントを処理します。

これは [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) で示されているトポロジーです。

より広い Realtime API でも一部のサーバーサイド制御パターンで `call_id` を使用しますが、このリポジトリで提供されているアタッチ例は SIP です。

## この SDK の対象外であるブラウザー WebRTC

アプリの主要クライアントが Realtime WebRTC を使用するブラウザーである場合:

-   このリポジトリの Python SDK ドキュメントの対象外として扱ってください。
-   クライアントサイドフローとイベントモデルについては、公式の [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/) と [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations/) のドキュメントを使用してください。
-   ブラウザー WebRTC クライアントに加えてサイドバンドのサーバー接続が必要な場合は、公式の [Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls/) ガイドを使用してください。
-   このリポジトリがブラウザーサイド `RTCPeerConnection` 抽象化や、すぐに使えるブラウザー WebRTC サンプルを提供することは期待しないでください。

このリポジトリには現在、ブラウザー WebRTC と Python サイドバンドを組み合わせた例も含まれていません。

## カスタムエンドポイントとアタッチポイント

[`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig] のトランスポート設定インターフェースにより、デフォルトパスを調整できます。

-   `url`: WebSocket エンドポイントを上書きします
-   `headers`: Azure 認証ヘッダーなどの明示的なヘッダーを提供します
-   `api_key`: API キーを直接、またはコールバック経由で渡します
-   `call_id`: 既存の realtime 通話にアタッチします。このリポジトリで文書化されている例は SIP です。
-   `playback_tracker`: 割り込み処理のために実際の再生進行を報告します

トポロジーを選択した後の詳細なライフサイクルと機能インターフェースについては、[Realtime agents guide](guide.md) を参照してください。