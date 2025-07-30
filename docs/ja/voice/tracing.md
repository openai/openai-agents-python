---
search:
  exclude: true
---
# トレーシング

エージェントのトレーシングと同様に、音声パイプラインも自動でトレーシングされます。

基本的なトレーシングについては上記のトレーシングドキュメントをご覧ください。加えて、 [`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を使用してパイプラインのトレーシングを設定できます。

主なトレーシング関連フィールドは次のとおりです。

- [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled] : トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。  
- [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data] : トレースに音声の書き起こしなど、機微なデータを含めるかどうかを制御します。これは音声パイプライン専用で、 Workflow 内部の処理には影響しません。  
- [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] : トレースに音声データを含めるかどうかを制御します。  
- [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name] : トレース Workflow の名前。  
- [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id] : 複数のトレースを関連付けるための `group_id` 。  
- [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled] : トレースに追加するメタデータ。