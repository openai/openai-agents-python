---
search:
  exclude: true
---
# リリースプロセス / 変更ログ

このプロジェクトは、`0.Y.Z` 形式を用いたセマンティックバージョニングをやや変更した方式に従っています。先頭の `0` は、 SDK がいまだ急速に進化していることを示します。各コンポーネントの増分ルールは以下のとおりです。

## マイナー (`Y`) バージョン

ベータでない公開インターフェースに **破壊的変更** が入る場合、マイナーバージョン `Y` を増やします。たとえば、`0.0.x` から `0.1.x` への移行には破壊的変更が含まれる可能性があります。

破壊的変更を避けたい場合は、プロジェクトで `0.0.x` バージョンに固定することをおすすめします。

## パッチ (`Z`) バージョン

互換性を損なわない変更の場合は `Z` を増やします。

-   バグ修正
-   新機能
-   非公開インターフェースの変更
-   ベータ機能の更新

## 破壊的変更の変更ログ

### 0.2.0

このバージョンでは、以前は引数に `Agent` を受け取っていたいくつかの箇所が、代わりに `AgentBase` を受け取るようになりました。例として、 MCP サーバーの `list_tools()` 呼び出しがあります。これは型に関する変更のみで、実際には引き続き `Agent` オブジェクトを受け取ります。更新する際は、`Agent` を `AgentBase` に置き換えて型エラーを解消してください。

### 0.1.0

このバージョンでは、[`MCPServer.list_tools()`][agents.mcp.server.MCPServer] に新しいパラメーター `run_context` と `agent` が追加されました。`MCPServer` を継承しているクラスには、これらのパラメーターを追加する必要があります。