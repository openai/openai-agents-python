---
search:
  exclude: true
---
# トレーシング

[エージェントのトレーシング](../tracing.md)と同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については上記ドキュメントを参照してください。さらに、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を使用してパイプラインのトレーシングを構成できます。

主要なトレーシング関連フィールドは次のとおりです。

- [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。
- [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声書き起こしなど、機微なデータをトレースに含めるかどうかを制御します。これは音声パイプラインに特有であり、ワークフロー内で行われる処理には適用されません。
- [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 音声データをトレースに含めるかどうかを制御します。
- [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースのワークフロー名。
- [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: トレースの `group_id`。複数のトレースを関連付けることができます。
- [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加のメタデータ。