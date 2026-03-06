---
search:
  exclude: true
---
# トレーシング

[エージェントがトレーシングされる](../tracing.md)のと同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については上記のトレーシングドキュメントを参照できますが、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を介してパイプラインのトレーシングを追加で設定することもできます。

トレーシングに関連する主要なフィールドは次のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効化するかどうかを制御します。デフォルトでは、トレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: トレースに、音声文字起こしのような潜在的に機微なデータを含めるかどうかを制御します。これは音声パイプライン専用であり、Workflow 内で行われるものには適用されません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: トレースに音声データを含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレース Workflow の名前です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: トレースの `group_id` で、複数のトレースを関連付けられます。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.trace_metadata]: トレースに含める追加のメタデータです。