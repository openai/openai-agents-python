---
search:
  exclude: true
---
# トレーシング

[エージェントのトレーシング](../tracing.md) と同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報は上記のドキュメントをご覧ください。加えて、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を介してパイプラインのトレーシングを設定できます。

トレーシング関連の主なフィールドは以下のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声の書き起こしなど、機微な可能性があるデータをトレースに含めるかどうかを制御します。これは音声パイプラインに固有の設定であり、ワークフロー内部で行われる処理には適用されません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: トレースに音声データを含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレース用ワークフローの名前です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 複数のトレースを関連付けるためのトレースの `group_id` です。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加のメタデータです。