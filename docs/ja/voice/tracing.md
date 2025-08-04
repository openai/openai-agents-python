---
search:
  exclude: true
---
# トレーシング

[エージェントがトレーシングされる](../tracing.md) のと同様に、voice パイプラインも自動的にトレーシングされます。

上記のトレーシングドキュメントで基本的な情報を確認できますが、`VoicePipelineConfig` を通じてパイプラインのトレーシングを追加で設定することもできます。

主なトレーシング関連フィールドは次のとおりです:

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声の書き起こしなど、機微なデータをトレースに含めるかどうかを制御します。これは voice パイプライン専用であり、Workflow 内部で行われる処理には影響しません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: トレースに音声データを含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースワークフローの名前です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: トレースの `group_id` で、複数のトレースをリンクすることができます。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加メタデータです。