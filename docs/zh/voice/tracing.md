---
search:
  exclude: true
---
# 追踪

与[智能体追踪](../tracing.md)类似，语音管线也会自动进行追踪。

你可以阅读上述追踪文档来了解基本的追踪信息，还可以通过 [`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] 配置管线追踪。

与追踪相关的主要字段包括：

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：控制是否禁用追踪。默认情况下，追踪处于启用状态。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]：控制语音管线 span 是否包含可能敏感的文本。当此字段为 `False` 时，转录 span 会省略转录文本、STT 提示词和 STT 关键词，而语音 span 会省略 TTS 输入文本和 TTS 指令。实际设置仍会发送给语音模型。此字段不控制你的工作流内部的追踪。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]：控制追踪是否包含音频数据。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]：追踪工作流的名称。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]：追踪的 `group_id`，可用于关联多个追踪。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.trace_metadata]：追踪中包含的其他元数据。