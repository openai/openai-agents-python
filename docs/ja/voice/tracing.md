---
search:
  exclude: true
---
# トレーシング

[エージェントのトレーシング](../tracing.md) と同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については上記ドキュメントをご覧ください。加えて、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を通じてパイプラインのトレーシングを設定できます。

主なトレーシング関連フィールドは次のとおりです。

- [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。デフォルトでは有効です。
- [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声書き起こしなど、機微な可能性のあるデータをトレースに含めるかどうかを制御します。これは音声パイプライン専用で、ワークフロー内部で行われる処理には適用されません。
- [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 音声データをトレースに含めるかどうかを制御します。
- [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースのワークフロー名です。
- [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 複数のトレースを関連付けるための、トレースの `group_id` です。
- [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加のメタデータです。