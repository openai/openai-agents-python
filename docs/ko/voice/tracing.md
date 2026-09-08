---
search:
  exclude: true
---
# 트레이싱

[에이전트가 트레이싱되는](../tracing.md) 방식과 마찬가지로 음성 파이프라인도 자동으로 트레이싱됩니다.

기본적인 트레이싱 정보는 위의 트레이싱 문서에서 확인할 수 있으며, [`VoicePipelineConfig`][agents.voice.pipeline_config.VoicePipelineConfig]을 통해 파이프라인의 트레이싱을 추가로 구성할 수도 있습니다.

트레이싱과 관련된 주요 필드는 다음과 같습니다.

-   [`tracing_disabled`][agents.voice.pipeline_config.VoicePipelineConfig.tracing_disabled]: 트레이싱을 비활성화할지 여부를 제어합니다. 기본적으로 트레이싱은 활성화됩니다.
-   [`trace_include_sensitive_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_data]: 음성 파이프라인 스팬에 잠재적으로 민감한 텍스트를 포함할지 여부를 제어합니다. 이 필드가 `False`이면 트랜스크립션 스팬에서 트랜스크립트, STT 프롬프트 및 STT 키워드가 제외되고, 음성 스팬에서 TTS 입력 텍스트 및 TTS 지침이 제외됩니다. 실제 설정은 여전히 음성 모델로 전송됩니다. 이 필드는 워크플로 내부의 트레이싱을 제어하지 않습니다.
-   [`trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]: 트레이스에 오디오 데이터를 포함할지 여부를 제어합니다.
-   [`workflow_name`][agents.voice.pipeline_config.VoicePipelineConfig.workflow_name]: 트레이스 워크플로의 이름입니다.
-   [`group_id`][agents.voice.pipeline_config.VoicePipelineConfig.group_id]: 여러 트레이스를 연결할 수 있게 해주는 트레이스의 `group_id`입니다.
-   [`trace_metadata`][agents.voice.pipeline_config.VoicePipelineConfig.trace_metadata]: 트레이스에 포함할 추가 메타데이터입니다.