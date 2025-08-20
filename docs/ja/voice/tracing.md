---
search:
  exclude: true
---
# トレーシング

[ エージェント のトレーシング](../tracing.md) と同様に、音声パイプラインも自動的にトレースされます。

基本的なトレーシング情報については上記のドキュメントをご参照ください。加えて、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を使ってパイプラインのトレーシングを構成できます。

トレーシングに関する主なフィールドは次のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。既定ではトレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: オーディオの書き起こしなど、機密になり得るデータをトレースに含めるかどうかを制御します。これは音声パイプライン専用であり、あなたの Workflow（ワークフロー） 内部で行われる処理には適用されません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: オーディオデータをトレースに含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースのワークフロー名。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 複数のトレースを関連付けるための `group_id`。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加のメタデータ。