# トレーシング

[エージェントのトレーシング](../tracing.md) と同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については、上記のトレーシングドキュメントを参照してください。さらに、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を使用してパイプラインのトレーシングを設定できます。

主なトレーシング関連フィールドは以下のとおりです。

- [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。
- [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]：音声文字起こしなど、機密性の高いデータをトレースに含めるかどうかを制御します。これは特に音声パイプライン向けであり、ワークフロー内部の処理には影響しません。
- [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]：トレースに音声データを含めるかどうかを制御します。
- [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]：トレースするワークフローの名前です。
- [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]：複数のトレースを関連付けるためのトレースの `group_id` です。
- [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：トレースに含める追加のメタデータです。