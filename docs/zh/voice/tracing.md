---
search:
  exclude: true
---
# 追踪

就像[智能体会被追踪](../tracing.md)一样，语音管线也会被自动追踪。

你可以阅读上面的追踪文档以了解基本的追踪信息；此外，你还可以通过[`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig]配置管线的追踪。

与追踪相关的关键字段包括：

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]：控制是否禁用追踪。默认情况下，追踪处于启用状态。
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]：控制追踪是否包含可能敏感的数据，例如音频转录文本。这专门针对语音管线，不适用于你的工作流内部发生的任何事情。
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]：控制追踪是否包含音频数据。
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]：追踪工作流的名称。
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]：追踪的`group_id`，可用于关联多个追踪。
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.trace_metadata]：要包含在追踪中的额外元数据。