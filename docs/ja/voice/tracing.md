---
search:
  exclude: true
---
# トレーシング

[エージェントがトレーシングされる方法](../tracing.md) と同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については上記のドキュメントをご覧ください。さらに、音声パイプラインのトレーシングは [`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を使用して設定できます。

主なトレーシング関連フィールドは次のとおりです:

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声の書き起こしなど、機密となり得るデータをトレースに含めるかどうかを制御します。これは音声パイプライン専用で、 Workflow 内部の処理には影響しません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: トレースに音声データを含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレース ワークフロー の名前です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 複数のトレースをリンクするために使用できる `group_id` です。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに追加するメタデータです。