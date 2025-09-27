---
search:
  exclude: true
---
# トレーシング

[エージェントのトレーシング](../tracing.md) と同様に、音声パイプラインも自動でトレーシングされます。

基本的なトレーシング情報は上記のドキュメントをご参照ください。さらに、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を使ってパイプラインのトレーシングを追加で設定できます。

主なトレーシング関連フィールド:

- [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかを制御します。既定では有効です。
- [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声書き起こしなど、機微なデータをトレースに含めるかを制御します。これは特に音声パイプライン向けであり、あなたの Workflow 内部で行われる処理には適用されません。
- [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 音声データをトレースに含めるかを制御します。
- [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースのワークフロー名です。
- [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 複数のトレースをリンクできる、そのトレースの `group_id` です。
- [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加のメタデータです。