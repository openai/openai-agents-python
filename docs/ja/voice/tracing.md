---
search:
  exclude: true
---
# トレーシング

[エージェントがトレーシングされる方法](../tracing.md) と同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については上記のドキュメントをご覧ください。加えて、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を通じてパイプラインのトレーシングを構成できます。

主要なトレーシング関連フィールドは次のとおりです:

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効化するかどうかを制御します。既定ではトレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声の書き起こしのような機微情報をトレースに含めるかどうかを制御します。これは音声パイプライン専用であり、あなたの Workflow 内で起こる処理には適用されません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: トレースに音声データを含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースのワークフロー名。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 複数のトレースを関連付けるためのトレースの `group_id`。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加のメタデータ。