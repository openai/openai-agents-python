---
search:
  exclude: true
---
# 追踪

与[智能体是如何被追踪的](../tracing.md)相同，语音流水线也会被自动追踪。

你可以参考上面的追踪文档获取基础信息，此外还可以通过[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig]对流水线的追踪进行配置。

与追踪相关的关键字段包括：

- [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：控制是否禁用追踪。默认情况下，追踪是启用的。
- [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]：控制追踪中是否包含潜在的敏感数据，例如音频转录文本。此设置仅适用于语音流水线，不适用于你的 Workflow 内部发生的任何事情。
- [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]：控制追踪中是否包含音频数据。
- [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]：追踪工作流的名称。
- [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]：追踪的`group_id`，可用于关联多个追踪。
- [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：需要随追踪一并包含的附加元数据。