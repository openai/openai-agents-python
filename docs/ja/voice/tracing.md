---
search:
  exclude: true
---
# トレーシング

[エージェントのトレーシング](../tracing.md) と同様に、音声パイプラインも自動的にトレースされます。

基本的なトレーシング情報については上記のトレーシングのドキュメントをご参照ください。加えて、`VoicePipelineConfig` を通じてパイプラインのトレーシングを構成できます。

主要なトレーシング関連フィールドは次のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効化するかどうかを制御します。既定ではトレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声書き起こしなど、機微情報になり得るデータをトレースに含めるかどうかを制御します。これは音声パイプラインに特有で、ワークフロー内で行われる処理には適用されません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 音声データをトレースに含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレース ワークフロー の名前。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 複数のトレースを関連付けられるようにする、トレースの `group_id` です。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加のメタデータ。