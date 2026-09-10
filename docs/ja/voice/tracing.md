---
search:
  exclude: true
---
# トレーシング

[エージェントのトレーシング](../tracing.md)と同様に、音声パイプラインも自動的にトレーシングされます。

トレーシングの基本情報については上記のドキュメントを参照してください。さらに、[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] を使用してパイプラインのトレーシングを設定できます。

トレーシングに関連する主なフィールドは次のとおりです。

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: トレーシングを無効にするかどうかを制御します。デフォルトでは、トレーシングは有効です。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 音声パイプラインのスパンに、機密情報である可能性のあるテキストを含めるかどうかを制御します。このフィールドが `False` の場合、文字起こしスパンでは文字起こし、STT プロンプト、STT キーワードが省略され、音声スパンでは TTS 入力テキストと TTS instructions が省略されます。実際の設定は引き続き音声モデルへ送信されます。このフィールドは、Workflow 内のトレーシングを制御しません。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: トレースに音声データを含めるかどうかを制御します。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: トレースワークフローの名前です。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: トレースの `group_id` です。これにより、複数のトレースを関連付けることができます。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.trace_metadata]: トレースに含める追加のメタデータです。