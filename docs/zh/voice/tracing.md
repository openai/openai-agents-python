---
search:
  exclude: true
---
# 追踪

就像[智能体如何被追踪](../tracing.md)一样，语音管道也会被自动追踪。

你可以阅读上面的追踪文档以了解基础追踪信息，但你还可以通过 [`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] 额外配置管道的追踪。

与追踪相关的关键字段包括：

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：控制是否禁用追踪。默认启用追踪。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]：控制追踪中是否包含潜在敏感数据，例如音频转写文本。这仅适用于语音管道，而不适用于你的 Workflow 内部发生的任何内容。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]：控制追踪中是否包含音频数据。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]：追踪工作流的名称。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]：追踪的 `group_id`，用于关联多个追踪记录。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.trace_metadata]：要随追踪一并包含的附加元数据。