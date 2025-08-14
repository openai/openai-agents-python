---
search:
  exclude: true
---
# トレーシング

[エージェントのトレーシング](../tracing.md) と同様に、音声パイプラインも自動でトレーシングされます。

基本的なトレーシング情報は上記ドキュメントをご参照ください。加えて、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を通じてパイプラインのトレーシングを構成できます。

トレーシングに関する主なフィールドは次のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効化するかどうかを制御します。既定ではトレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: オーディオの書き起こしなど、機微なデータをトレースに含めるかどうかを制御します。これは音声パイプライン専用であり、 Workflow 内部で行われる処理には適用されません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 音声データをトレースに含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレース用 Workflow の名前です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 複数のトレースを関連付けるためのトレースの `group_id` です。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加のメタデータです。