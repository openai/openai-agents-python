---
search:
  exclude: true
---
# 追踪

与[智能体如何被追踪](../tracing.md)相同，语音流水线也会被自动追踪。

你可以阅读上面的追踪文档以获取基础信息，此外还可以通过[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig]对流水线的追踪进行配置。

与追踪相关的关键字段包括：

- [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: 控制是否禁用追踪。默认启用追踪。
- [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 控制追踪是否包含可能的敏感数据，例如音频转写。该设置仅针对语音流水线，不影响你的工作流内部发生的任何内容。
- [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 控制追踪是否包含音频数据。
- [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: 追踪工作流名称。
- [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 追踪的`group_id`，用于关联多个追踪。
- [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: 需要随追踪一同包含的附加元数据。