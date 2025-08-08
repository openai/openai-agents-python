---
search:
  exclude: true
---
# トレーシング

[エージェントがトレーシングされる](../tracing.md) のと同様に、音声パイプラインも自動的にトレーシングされます。

上記のトレーシングドキュメントを参照すると基本的な情報を確認できますが、`VoicePipelineConfig` を使用してパイプラインのトレーシングを追加設定することも可能です。

トレーシングに関連する主なフィールドは次のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声の書き起こしなど、機微なデータをトレースに含めるかどうかを制御します。これは音声パイプラインにのみ適用され、あなたの Workflow 内で行われる処理には影響しません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 音声データをトレースに含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースワークフローの名前です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: トレースの `group_id` で、複数のトレースをリンクできます。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加メタデータです。