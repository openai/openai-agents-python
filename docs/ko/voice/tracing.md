---
search:
  exclude: true
---
# 트레이싱

[에이전트가 트레이싱되는 방식](../tracing.md)과 마찬가지로, 음성 파이프라인도 자동으로 트레이싱됩니다.

기본적인 트레이싱 정보는 위의 문서를 참고하시면 되며, 추가로 [`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig]를 통해 파이프라인의 트레이싱을 구성할 수 있습니다.

주요 트레이싱 관련 필드는 다음과 같습니다:

- [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: 트레이싱 비활성화 여부를 제어합니다. 기본값은 활성화입니다.
- [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 오디오 전사본과 같은 잠재적으로 민감한 데이터를 트레이스에 포함할지 제어합니다. 이는 음성 파이프라인에만 적용되며, 워크플로 내부에서 발생하는 작업에는 적용되지 않습니다.
- [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 트레이스에 오디오 데이터를 포함할지 제어합니다.
- [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: 트레이스 워크플로 이름
- [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 트레이스의 `group_id`로, 여러 트레이스를 연결할 수 있습니다.
- [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: 트레이스에 포함할 추가 메타데이터