---
search:
  exclude: true
---
# トレーシング

エージェントが [トレーシングされる](../tracing.md) のと同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシングの情報については上記のドキュメントをご覧いただけますが、` VoicePipelineConfig ` を使用してパイプラインのトレーシングを追加で設定することもできます。

トレーシングに関連する主なフィールドは次のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。  
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]：オーディオの書き起こしなど、機密になり得るデータをトレースに含めるかどうかを制御します。これは音声パイプライン専用であり、 Workflow 内で行われる処理には影響しません。  
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]：トレースにオーディオデータを含めるかどうかを制御します。  
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]：トレース Workflow の名前です。  
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]：複数のトレースをリンクするための `group_id` です。  
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：トレースに追加するメタデータです。