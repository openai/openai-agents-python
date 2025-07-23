---
search:
  exclude: true
---
# トレーシング

[エージェント](../tracing.md) と同様に、 voice パイプラインも自動でトレーシングされます。

基本的なトレーシングの情報については上記のドキュメントを参照してください。さらに、 `VoicePipelineConfig` を使用してパイプラインのトレーシングを設定できます。

トレーシングに関する主なフィールドは次のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。  
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]：音声の文字起こしなど、機微なデータをトレースに含めるかどうかを制御します。これは voice パイプライン専用であり、Workflow 内部の処理には影響しません。  
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]：音声データ自体をトレースに含めるかどうかを制御します。  
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]：トレースの Workflow 名です。  
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]：複数のトレースをリンクするための `group_id` です。  
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：トレースに追加するメタデータです。