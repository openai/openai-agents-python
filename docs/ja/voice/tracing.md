---
search:
  exclude: true
---
# トレーシング

[エージェントのトレーシング](../tracing.md) と同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については上記のドキュメントをご確認ください。加えて、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を使ってパイプラインのトレーシングを設定できます。

トレーシングに関係する主なフィールドは次のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効化するかを制御します。デフォルトではトレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声書き起こしなど、機密となり得るデータをトレースに含めるかを制御します。これは音声パイプライン専用で、ワークフロー内部で行われることには適用されません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 音声データをトレースに含めるかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースのワークフロー名です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 複数のトレースをリンクできる、トレースの `group_id` です。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加のメタデータです。