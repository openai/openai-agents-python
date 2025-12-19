---
search:
  exclude: true
---
# 追踪

与[智能体的追踪方式](../tracing.md)相同，语音管线也会被自动追踪。

你可以参考上面的追踪文档获取基础信息，此外你还可以通过[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig]来配置管线的追踪。

关键的追踪相关字段有：

- [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：控制是否禁用追踪。默认情况下启用追踪。
- [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]：控制追踪是否包含潜在的敏感数据，如音频转录文本。该设置仅作用于语音管线，不影响你的工作流内部发生的任何内容。
- [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]：控制追踪是否包含音频数据。
- [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]：追踪工作流的名称。
- [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]：追踪的`group_id`，用于关联多个追踪。
- [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：要随追踪一并包含的附加元数据。