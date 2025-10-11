---
search:
  exclude: true
---
# 追踪

与[智能体追踪](../tracing.md)类似，语音管道也会自动被追踪。

您可以阅读上述追踪文档了解基本的追踪信息，但您还可以通过 [`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig] 额外配置管道的追踪。

主要的追踪相关字段包括：

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: 控制是否禁用追踪。默认情况下，追踪是启用的。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 控制追踪是否包含潜在敏感数据，如音频转录。这专门针对语音管道，而不适用于您工作流内部进行的任何处理。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 控制追踪是否包含音频数据。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: 追踪工作流的名称。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 追踪的 `group_id`，让您可以链接多个追踪。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: 要包含在追踪中的额外元数据。