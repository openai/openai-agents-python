# トレーシング

[エージェントがトレースされる](../tracing.md)のと同様に、音声パイプラインも自動的にトレースされます。

基本的なトレーシング情報については上記のトレーシングドキュメントを参照できますが、パイプラインのトレーシングは [`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を通じて追加で設定できます。

トレーシングに関連する主なフィールドは次のとおりです：

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。デフォルトでは、トレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: トレースに音声トランスクリプトのような潜在的に機密性のあるデータを含めるかどうかを制御します。これは特に音声パイプライン用であり、ワークフロー内で行われることには関係ありません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: トレースに音声データを含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースワークフローの名前です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 複数のトレースをリンクするための `group_id` です。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加のメタデータです。