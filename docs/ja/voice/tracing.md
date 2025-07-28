---
search:
  exclude: true
---
# トレーシング

[エージェントのトレーシング方法](../tracing.md) と同様に、 voice パイプラインも自動的にトレーシングされます。

基本的なトレーシング情報については上記のドキュメントをご覧いただけますが、パイプラインのトレーシングは [`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を使って追加設定できます。

主なトレーシング関連フィールドは次のとおりです:

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。デフォルトではトレーシングは有効です。  
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: トレースに音声の書き起こしなどの機微データを含めるかどうかを制御します。これは特に voice パイプライン向けで、ユーザーの Workflow 内で行われる処理には影響しません。  
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: トレースに音声データを含めるかどうかを制御します。  
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースの Workflow 名です。  
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 複数のトレースをリンクさせるための `group_id` です。  
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレースに追加するメタデータです。