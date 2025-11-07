---
search:
  exclude: true
---
# トレーシング

[エージェントがトレースされる方法](../tracing.md) と同様に、音声パイプラインも自動的にトレースされます。

基本的なトレーシング情報は上記のトレーシングのドキュメントをご参照ください。加えて、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] でパイプラインのトレーシングを構成できます。

トレーシング関連の主なフィールドは次のとおりです:

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効化するかどうかを制御します。デフォルトではトレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: トレースに音声書き起こしなどの機微なデータを含めるかどうかを制御します。これは特に音声パイプライン向けの設定であり、ワークフロー内部で行われる処理には適用されません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: トレースに音声データを含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースのワークフロー名です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: トレースの `group_id` です。複数のトレースを関連付けるために使用できます。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加のメタデータです。