---
search:
  exclude: true
---
# トレーシング

[エージェントのトレーシング](../tracing.md)と同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については上記のトレーシングドキュメントを参照できますが、さらに [`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を通じてパイプラインのトレーシングを設定できます。

トレーシングに関連する主なフィールドは次のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。デフォルトでは、トレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: トレースに音声文字起こしなど、潜在的に機微なデータを含めるかどうかを制御します。これは音声パイプライン専用であり、Workflow 内で行われる処理には適用されません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: トレースに音声データを含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースワークフローの名前です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: トレースの `group_id` で、複数のトレースを関連付けることができます。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.trace_metadata]: トレースに含める追加のメタデータです。