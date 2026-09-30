---
search:
  exclude: true
---
# 트레이싱

Agents SDK에는 트레이싱이 기본 제공되며, 에이전트 실행 중 발생하는 LLM 생성, 도구 호출, 핸드오프, 가드레일, 사용자 지정 이벤트까지 포괄적으로 기록합니다. [트레이스 대시보드](https://platform.openai.com/traces)를 사용하면 개발 및 프로덕션 환경에서 워크플로를 디버깅하고 시각화하며 모니터링할 수 있습니다.

!!!note

    트레이싱은 기본적으로 활성화되어 있습니다. 일반적으로 다음 세 가지 방법으로 비활성화할 수 있습니다.

    1. 환경 변수 `OPENAI_AGENTS_DISABLE_TRACING=1`을 설정하여 트레이싱을 전역적으로 비활성화할 수 있습니다
    2. 코드에서 [`set_tracing_disabled(True)`][agents.set_tracing_disabled]을 사용하여 트레이싱을 전역적으로 비활성화할 수 있습니다
    3. [`agents.run.RunConfig.tracing_disabled`][]을 `True`으로 설정하여 단일 실행의 트레이싱을 비활성화할 수 있습니다

***Zero Data Retention(ZDR) 정책에 따라 OpenAI API를 사용하는 조직에서는 트레이싱을 사용할 수 없습니다.***

## 트레이스와 스팬 {#traces-and-spans}

- **트레이스**는 하나의 "워크플로"에 대한 단일 엔드투엔드 작업을 나타냅니다. 여러 스팬으로 구성되며 다음 속성을 갖습니다.
    - `workflow_name`: 논리적 워크플로나 앱의 이름입니다. 예를 들면 "코드 생성" 또는 "고객 서비스"입니다.
    - `trace_id`: 트레이스의 고유 ID입니다. 전달하지 않으면 자동으로 생성됩니다. 형식은 `trace_<32_alphanumeric>`이어야 합니다.
    - `group_id`: 동일한 대화의 여러 트레이스를 연결하는 선택적 그룹 ID입니다. 예를 들어 채팅 스레드 ID를 사용할 수 있습니다.
    - `disabled`: True이면 트레이스가 기록되지 않습니다.
    - `metadata`: 트레이스의 선택적 메타데이터입니다.
- **스팬**은 시작 및 종료 시간이 있는 작업을 나타냅니다. 스팬에는 다음 항목이 있습니다.
    - `started_at` 및 `ended_at` 타임스탬프
    - 스팬이 속한 트레이스를 나타내는 `trace_id`
    - 이 스팬의 상위 스팬을 가리키는 `parent_id`(있는 경우)
    - 스팬에 관한 정보인 `span_data`. 예를 들어 `AgentSpanData`에는 에이전트에 관한 정보가 포함되고, `GenerationSpanData`에는 LLM 생성에 관한 정보가 포함됩니다.

## 기본 트레이싱 {#default-tracing}

SDK는 기본적으로 다음 항목을 트레이싱합니다.

- 전체 `Runner.{run, run_sync, run_streamed}()`은 `trace()`으로 래핑됩니다.
- 각 러너 호출은 `task_span()`으로 래핑됩니다.
- 각 모델 턴은 `turn_span()`으로 래핑됩니다.
- 에이전트가 실행될 때마다 `agent_span()`로 래핑됩니다
- LLM 생성은 `generation_span()`로 래핑됩니다
- 각 함수 도구 호출은 `function_span()`으로 래핑됩니다
- 가드레일은 `guardrail_span()`로 래핑됩니다
- 핸드오프는 `handoff_span()`로 래핑됩니다
- 오디오 입력(음성 텍스트 변환)은 `transcription_span()`으로 래핑됩니다
- 오디오 출력(텍스트 음성 변환)은 `speech_span()`로 래핑됩니다
- SDK는 관련 오디오 스팬을 `speech_group_span()`의 하위에 배치할 수 있습니다

기본 트레이스 이름은 리터럴 문자열 `Agent workflow`입니다. `trace`을 사용하면 이 이름을 설정할 수 있으며, [`RunConfig`][agents.run.RunConfig]을 사용하면 이름과 기타 속성을 구성할 수 있습니다.

더 간결한 계층 구조가 필요하다면 실행의 자동 작업 및 턴 스팬을 비활성화하십시오. 에이전트, 생성, 함수, 가드레일, 핸드오프 및 사용자 지정 스팬은 계속 기록됩니다.

```python
from agents import RunConfig, Runner

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(tracing={"include_task_and_turn_spans": False}),
)
```

또한 [사용자 지정 트레이스 프로세서](#custom-tracing-processors)를 설정하여 트레이스를 다른 대상으로 전송할 수 있습니다. 이 대상은 기존 대상을 대체하거나 보조 대상으로 사용할 수 있습니다.

## 장기 실행 워커와 즉시 내보내기 {#long-running-workers-and-immediate-exports}

기본 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]는 몇 초마다 백그라운드에서 트레이스를 내보내며, 메모리 내 큐가 크기 트리거에 도달하면 그보다 일찍 내보냅니다. 또한 프로세스가 종료될 때 최종 플러시를 수행합니다. Celery, RQ, Dramatiq 또는 FastAPI 백그라운드 작업과 같은 장기 실행 워커에서는 추가 코드 없이도 일반적으로 트레이스가 자동으로 내보내지지만, 각 작업이 완료된 직후 트레이스 대시보드에 표시되지 않을 수 있습니다.

작업 단위가 끝날 때 즉시 전달되는 것을 보장해야 한다면 트레이스 컨텍스트가 종료된 후 [`flush_traces()`][agents.tracing.flush_traces]을 호출하십시오.

```python
from agents import Runner, flush_traces, trace


@celery_app.task
def run_agent_task(prompt: str):
    try:
        with trace("celery_task"):
            result = Runner.run_sync(agent, prompt)
        return result.final_output
    finally:
        flush_traces()
```

```python
from fastapi import BackgroundTasks, FastAPI
from agents import Runner, flush_traces, trace

app = FastAPI()


def process_in_background(prompt: str) -> None:
    try:
        with trace("background_job"):
            Runner.run_sync(agent, prompt)
    finally:
        flush_traces()


@app.post("/run")
async def run(prompt: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_in_background, prompt)
    return {"status": "queued"}
```

[`flush_traces()`][agents.tracing.flush_traces]은 현재 버퍼링된 트레이스와 스팬이 내보내질 때까지 차단되므로, 일부만 구성된 트레이스를 플러시하지 않도록 `trace()`가 닫힌 후 호출하십시오. 기본 내보내기 지연 시간이 허용되는 경우에는 이 호출을 생략할 수 있습니다.

트레이싱을 비활성화하면 기본 제공자가 새 트레이스와 스팬을 생성하지 않지만, 프로세서가 이미 버퍼링한 데이터는 삭제되지 않습니다. `set_tracing_disabled(True)` 또는 `OPENAI_AGENTS_DISABLE_TRACING=1`을 통해 트레이싱을 비활성화한 후에도 [`flush_traces()`][agents.tracing.flush_traces]은 해당 버퍼 데이터를 계속 플러시합니다.

## 상위 수준 트레이스 {#higher-level-traces}

경우에 따라 여러 `run()` 호출을 단일 트레이스에 포함하고 싶을 수 있습니다. 전체 코드를 `trace()`으로 래핑하면 됩니다.

```python
from agents import Agent, Runner, trace

async def main():
    agent = Agent(name="Joke generator", instructions="Tell funny jokes.")

    with trace("Joke workflow"): # (1)!
        first_result = await Runner.run(agent, "Tell me a joke")
        second_result = await Runner.run(agent, f"Rate this joke: {first_result.final_output}")
        print(f"Joke: {first_result.final_output}")
        print(f"Rating: {second_result.final_output}")
```

1. 두 `Runner.run` 호출이 `with trace()`으로 래핑되므로, 각 실행이 별도의 트레이스를 생성하는 대신 두 실행 모두 하나의 전체 트레이스에 포함됩니다.

## 트레이스 생성 {#creating-traces}

[`trace()`][agents.tracing.trace] 함수를 사용하여 트레이스를 생성할 수 있습니다. 트레이스는 시작하고 종료해야 합니다. 다음 두 가지 방법을 사용할 수 있습니다.

1. **권장**: 트레이스를 컨텍스트 관리자로 사용합니다. 즉, `with trace(...) as my_trace`을 사용합니다. 그러면 적절한 시점에 트레이스가 자동으로 시작되고 종료됩니다.
2. [`trace.start()`][agents.tracing.Trace.start]와 [`trace.finish()`][agents.tracing.Trace.finish]를 직접 호출할 수도 있습니다.

현재 트레이스는 Python [`contextvar`](https://docs.python.org/3/library/contextvars.html)를 통해 추적됩니다. 따라서 동시성 환경에서도 자동으로 작동합니다. 트레이스를 직접 시작하고 종료하는 경우 현재 트레이스를 업데이트하려면 `start()`에 `mark_as_current`을 전달하고, `finish()`에 `reset_current`을 전달하십시오.

## 스팬 생성 {#creating-spans}

다양한 [`*_span()`][agents.tracing.create] 메서드를 사용하여 스팬을 생성할 수 있습니다. 일반적으로 스팬을 직접 생성할 필요는 없습니다. 사용자 지정 스팬 정보를 추적하기 위한 [`custom_span()`][agents.tracing.custom_span] 함수가 제공됩니다.

스팬은 자동으로 현재 트레이스에 포함되며, Python [`contextvar`](https://docs.python.org/3/library/contextvars.html)를 통해 추적되는 가장 가까운 현재 스팬의 하위에 배치됩니다.

## 민감한 데이터 {#sensitive-data}

일부 스팬은 잠재적으로 민감한 데이터를 캡처할 수 있습니다.

`generation_span()`는 LLM 생성의 입력과 출력을 저장하고, `function_span()`은 함수 호출의 입력과 출력을 저장합니다. 여기에는 민감한 데이터가 포함될 수 있으므로 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]을 통해 해당 데이터 캡처를 비활성화할 수 있습니다.

승인이 필요한 함수 도구의 경우, 승인을 위해 일시 중지된 스팬은 SDK의 내부 결과 래퍼를 도구 출력으로 저장하지 않습니다. 애플리케이션이 사용자 지정 거부 메시지와 함께 호출을 거부하면 `trace_include_sensitive_data`이 `True`일 때만 함수 스팬이 해당 메시지를 출력 및 오류 텍스트로 저장합니다. 설정이 `False`이면 스팬은 출력을 생략하고 일반 오류 텍스트 `Tool execution rejected`을 사용합니다.

마찬가지로 오디오 스팬에는 기본적으로 입력 및 출력 오디오의 base64 인코딩 PCM 데이터가 포함됩니다. [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]을 구성하여 이 오디오 데이터 캡처를 비활성화할 수 있습니다.

기본적으로 `trace_include_sensitive_data`은 `True`입니다. 앱을 실행하기 전에 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 환경 변수를 `true/1` 또는 `false/0`로 내보내면 코드 없이 기본값을 설정할 수 있습니다.

`trace_include_sensitive_data`이 `False`이면 Responses 모델 스팬에서 요청 입력과 응답 출력이 생략됩니다. 공식 OpenAI 엔드포인트 호출의 경우 스팬에 상관관계 메타데이터로 Responses API `response_id`이 계속 포함됩니다. 사용자 지정 엔드포인트의 민감 정보가 제거된 스팬에서는 SDK가 해당 식별자를 생략합니다.

## 사용자 지정 트레이싱 프로세서 {#custom-tracing-processors}

트레이싱의 상위 수준 아키텍처는 다음과 같습니다.

- 초기화 시 트레이스 생성을 담당하는 전역 [`TraceProvider`][agents.tracing.provider.TraceProvider]을 생성합니다.
- 트레이스와 스팬을 배치 단위로 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]에 보내는 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]로 `TraceProvider`을 구성합니다. 이 익스포터는 스팬과 트레이스를 OpenAI 백엔드로 배치 단위로 내보냅니다.

이 기본 설정을 사용자 지정하여 트레이스를 대체 또는 추가 백엔드로 보내거나 익스포터 동작을 수정하는 방법은 두 가지입니다.

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]을 사용하면 준비된 트레이스와 스팬을 수신하는 **추가** 트레이스 프로세서를 추가할 수 있습니다. 이를 통해 트레이스를 OpenAI 백엔드로 보내는 동시에 자체 처리를 수행할 수 있습니다.
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]을 사용하면 기본 프로세서를 자체 트레이스 프로세서로 **대체**할 수 있습니다. 이 경우 트레이스를 전송하는 `TracingProcessor`을 포함하지 않으면 트레이스가 OpenAI 백엔드로 전송되지 않습니다.

### 내보내기 전 민감 정보 제거 {#redaction-before-export}

트레이스 프로세서는 서로 독립적인 관찰자입니다. 기본 제공자는 프로세서의 콜백 예외를 포착하고 등록된 다른 프로세서를 계속 호출합니다. 따라서 익스포터보다 먼저 등록된 민감 정보 제거 프로세서에서 처리가 실패하더라도 해당 익스포터의 데이터 수신을 막지는 못합니다. `add_trace_processor()`을 사용하여 프로세서를 추가해도 기본 OpenAI 익스포터는 등록된 상태로 유지됩니다.

성공적인 민감 정보 제거가 내보내기의 전제 조건이라면 민감 정보 제거와 전달을 애플리케이션이 소유한 동일한 익스포터 내부에서 처리하십시오. `set_trace_processors()`을 사용하여 기본 프로세서를 해당 익스포터로 구성된 `BatchTraceProcessor`으로 대체하십시오. 익스포터는 직렬화된 페이로드를 복사하고, 복사본에서 민감 정보를 제거한 후, 처리된 결과만 대상으로 전달해야 합니다. 직렬화, 복사 또는 민감 정보 제거에 실패하면 대상을 호출하기 전에 해당 배치를 폐기하십시오. 페이로드, 예외 텍스트 또는 트레이스백을 포함하지 않는 고정된 실패 메시지를 기록하십시오.

[트레이스 민감 정보 제거 예제](https://github.com/openai/openai-agents-python/blob/main/examples/basic/trace_redaction.py)는 기존 트레이싱 API를 사용하여 이러한 구성을 구현하는 방법을 보여 줍니다. 이 예제는 이벤트 카테고리와 트레이스/스팬 연결 ID만 로컬 콘솔에 출력하며 API 호출은 수행하지 않습니다. 허용 목록에서는 이름, 메타데이터, 오류 및 스팬 데이터를 제외합니다. 호출자가 제공하는 ID에는 민감한 정보가 없어야 하며, 그렇지 않으면 애플리케이션이 해당 ID를 안전한 값에 매핑해야 합니다. 이 진단 출력은 OpenAI 트레이싱 수집 스키마가 아닙니다. 백엔드로 데이터를 보내는 애플리케이션은 해당 백엔드와 호환되는 민감 정보 제거 정책과 대상을 제공해야 합니다.

민감 정보 제거기와 대상은 신뢰할 수 있는 애플리케이션 코드입니다. 원본 데이터를 별도로 기록하거나 전송해서는 안 됩니다. 배치 프로세서는 백그라운드 내보내기, 명시적 플러시 또는 종료 중에 익스포터를 호출할 수 있으므로 콜백은 이러한 실행 컨텍스트에서 안전하게 사용할 수 있어야 합니다. 실패한 배치는 삭제되지만 이후 배치는 계속 내보낼 수 있습니다. 프로세서 대체는 이후의 프로세서 콜백에 영향을 주며, 이전에 등록된 프로세서가 이미 버퍼링한 데이터는 삭제하지 않습니다. 트레이스를 생성하거나 에이전트를 실행하기 전에 대체 프로세서를 구성하십시오.


## OpenAI 외 모델을 사용한 트레이싱 {#tracing-with-non-openai-models}

OpenAI 외 모델을 사용할 때 트레이싱을 비활성화하지 않고도 OpenAI 트레이스 대시보드에서 무료 트레이싱을 사용하려면 트레이싱 익스포터에 OpenAI API 키를 제공할 수 있습니다. 어댑터 선택 및 설정 시 주의 사항은 모델 가이드의 [서드 파티 어댑터](models/index.md#third-party-adapters) 섹션을 참조하십시오.

```python
import os
from agents import set_tracing_export_api_key, Agent
from agents.extensions.models.any_llm_model import AnyLLMModel

tracing_api_key = os.environ["OPENAI_API_KEY"]
set_tracing_export_api_key(tracing_api_key)

model = AnyLLMModel(
    model="your-provider/your-model-name",
    api_key="your-api-key",
)

agent = Agent(
    name="Assistant",
    model=model,
)
```

단일 실행에만 다른 트레이싱 키가 필요하다면 전역 익스포터를 변경하는 대신 `RunConfig`을 통해 전달하십시오.

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 추가 참고 사항 {#additional-notes}
- OpenAI 트레이스 대시보드에서 무료 트레이스를 확인할 수 있습니다.


## 에코시스템 통합 {#ecosystem-integrations}

다음 커뮤니티 및 공급업체 통합은 OpenAI Agents SDK의 트레이싱 API 인터페이스를 지원합니다.

통합 유지관리자가 각 통합을 지원합니다. 이 목록에 포함되었다고 해서 OpenAI의 보증이나 보안 인증을 받았다는 의미는 아닙니다. 새 등재를 요청하려면 [통합 등재 기준](https://github.com/openai/openai-agents-python/blob/main/CONTRIBUTING.md#tracing-integration-listings)을 따르십시오.

### 외부 트레이싱 프로세서 목록 {#external-tracing-processors-list}

- [Weights & Biases](https://docs.wandb.ai/weave/guides/integrations/agents/openai-agents-sdk)
- [Arize Phoenix](https://arize.com/docs/phoenix/integrations/llm-providers/openai/openai-agents-sdk-tracing)
- [Future AGI](https://docs.futureagi.com/docs/tracing/auto/openai_agents/)
- [MLflow(자체 호스팅/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
- [MLflow(Databricks 호스팅)](https://docs.databricks.com/aws/en/mlflow3/genai/tracing/integrations/openai-agent)
- [Braintrust](https://www.braintrust.dev/docs/integrations/agent-frameworks/openai-agents-sdk)
- [Pydantic Logfire](https://pydantic.dev/docs/logfire/integrations/llms/openai/#openai-agents)
- [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
- [Scorecard](https://docs.scorecard.io/features/tracing#agent-frameworks)
- [Respan](https://www.respan.ai/docs/integrations/openai-agents-sdk)
- [LangSmith](https://docs.langchain.com/langsmith/trace-openai)
- [Maxim AI](https://www.getmaxim.ai/docs/sdk/python/integrations/openai/agents-sdk)
- [Comet Opik](https://www.comet.com/docs/opik/integrations/openai_agents)
- [Langfuse](https://langfuse.com/integrations/frameworks/openai-agents)
- [Langtrace](https://docs.langtrace.ai/supported-integrations/llm-frameworks/openai-agents-sdk)
- [Okahu-Monocle](https://github.com/monocle2ai/monocle)
- [Galileo](https://docs.galileo.ai/how-to-guides/third-party-integrations/openai-agent-integration)
- [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)
- [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk/)
- [Agenta](https://agenta.ai/docs/observability/integrations/openai-agents)
- [PostHog](https://posthog.com/docs/ai-observability/installation/openai-agents)
- [Traccia](https://traccia.ai/docs/integrations/openai-agents/)
- [PromptLayer](https://docs.promptlayer.com/features/observability/traces/integrations#openai-agents-sdk)
- [HoneyHive](https://docs.honeyhive.ai/v2/integrations/openai-agents)
- [Asqav](https://www.asqav.com/docs/integrations#openai-agents)
- [Datadog](https://docs.datadoghq.com/llm_observability/instrumentation/auto_instrumentation/?tab=python#openai-agents)
- [Latitude](https://docs.latitude.so/telemetry/frameworks/openai-agents)
- [DProvenanceKit](https://dprovenance.dev/openai-agents/)
- [Tuning Engines](https://github.com/cerebrixos-org/tuning-engines-cli/tree/main/packages/tuning-agents#openai-agents-sdk)
- [Laminar](https://laminar.sh/docs/tracing/integrations/openai-agents-sdk)
- [Noveum](https://github.com/Noveum/noveum-trace/blob/main/docs/OPENAI_AGENTS_INTEGRATION.md)