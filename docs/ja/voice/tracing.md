---
search:
  exclude: true
---
# トレーシング

[エージェントがトレーシングされる](../tracing.md)のと同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については上記のドキュメントをご覧ください。さらに、 `VoicePipelineConfig` を使ってパイプラインのトレーシングを設定することもできます。

トレーシングに関わる主なフィールドは次のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：トレーシングを無効にするかどうかを制御します。デフォルトでは有効です。  
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]：音声の書き起こしのような機密データをトレースに含めるかどうかを制御します。これは音声パイプライン専用で、Workflow 内部の処理には影響しません。  
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]：音声データ自体をトレースに含めるかどうかを制御します。  
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]：トレースワークフローの名前です。  
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]：複数のトレースをリンクできる `group_id` です。  
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：トレースに追加するメタデータです。