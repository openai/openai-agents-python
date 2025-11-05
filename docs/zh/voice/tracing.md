---
search:
  exclude: true
---
# 追踪

与[智能体的追踪](../tracing.md)方式相同，语音管道也会被自动追踪。

你可以阅读上面的追踪文档以了解基础信息，此外还可以通过[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig]对管道的追踪进行配置。

与追踪相关的关键字段包括：

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：控制是否禁用追踪。默认启用追踪。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]：控制追踪中是否包含可能的敏感数据，例如音频转写文本。该配置仅作用于语音管道，不影响你的工作流（Workflow）内部的内容。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]：控制追踪中是否包含音频数据。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]：追踪工作流的名称。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]：追踪的`group_id`，可用于关联多个追踪。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：随追踪一同包含的附加元数据。