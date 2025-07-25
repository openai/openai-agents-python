---
search:
  exclude: true
---
# トレーシング

[エージェントのトレーシング](../tracing.md) と同様に、音声パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については上記のドキュメントをご参照ください。さらに、 `VoicePipelineConfig` を通じてパイプラインのトレーシングを追加で設定できます。

主なトレーシング関連フィールドは次のとおりです:

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。  
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声の書き起こしなど、潜在的に機密性の高いデータをトレースに含めるかどうかを制御します。これは音声パイプライン専用であり、 Workflow 内部の処理には影響しません。  
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: トレースに音声データを含めるかどうかを制御します。  
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレース Workflow の名前です。  
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: トレースの `group_id` で、複数のトレースを関連付けることができます。  
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに含める追加メタデータです。