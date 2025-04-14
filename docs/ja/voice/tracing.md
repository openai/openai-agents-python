# トレーシング

[エージェントのトレーシング](../tracing.md)と同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については上記のトレーシングドキュメントをご参照いただけますが、さらに [`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を使ってパイプラインのトレーシングを設定することも可能です。

主なトレーシング関連フィールドは以下の通りです：

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]：トレースに音声の書き起こしなど、機密性の高いデータを含めるかどうかを制御します。これは特に音声パイプライン用であり、Workflow 内部で発生する内容には適用されません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]：トレースに音声データを含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]：トレースワークフローの名前です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]：トレースの `group_id` で、複数のトレースをリンクすることができます。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：トレースに追加するメタデータです。