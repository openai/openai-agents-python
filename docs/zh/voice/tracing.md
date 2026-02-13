---
search:
  exclude: true
---
# 追踪

与[智能体的追踪方式](../tracing.md)相同，语音管线也会被自动追踪。

你可以阅读上面的追踪文档以了解基础信息，此外还可以通过[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig]对管线的追踪进行配置。

关键的追踪相关字段包括：

- [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: 控制是否禁用追踪。默认启用追踪。
- [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 控制追踪是否包含潜在敏感数据，如音频转写。这仅适用于语音管线，不涉及你的工作流内部发生的任何内容。
- [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 控制追踪是否包含音频数据。
- [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: 跟踪工作流的名称。
- [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 跟踪的`group_id`，可用于关联多个跟踪。
- [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: 要随跟踪一起包含的其他元数据。