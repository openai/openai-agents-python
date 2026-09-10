---
search:
  exclude: true
---
# 트레이싱

Agents SDK에는 에이전트 실행 중 발생하는 LLM 생성, 도구 호출, 핸드오프, 가드레일은 물론 커스텀 이벤트까지 포괄적으로 기록하는 트레이싱 기능이 내장되어 있습니다. [트레이스 대시보드](https://platform.openai.com/traces)를 사용하면 개발 및 프로덕션 환경에서 워크플로를 디버깅하고 시각화하며 모니터링할 수 있습니다.

!!!note

    트레이싱은 기본적으로 활성화되어 있습니다. 일반적으로 다음 세 가지 방법으로 비활성화할 수 있습니다:

    1. 환경 변수 `OPENAI_AGENTS_DISABLE_TRACING=1`을 설정하여 트레이싱을 전역으로 비활성화할 수 있습니다
    2. [`set_tracing_disabled(True)`][agents.set_tracing_disabled]을 사용하여 코드에서 트레이싱을 전역으로 비활성화할 수 있습니다
    3. [`agents.run.RunConfig.tracing_disabled`][]를 `True`으로 설정하여 단일 실행의 트레이싱을 비활성화할 수 있습니다

***제로 데이터 보존(Zero Data Retention, ZDR) 정책에 따라 OpenAI API를 사용하는 조직에서는 트레이싱을 사용할 수 없습니다.***

## 트레이스와 스팬 {#traces-and-spans}

-   **트레이스**는 하나의 "워크플로"에서 처음부터 끝까지 수행되는 단일 작업을 나타냅니다. 트레이스는 여러 스팬으로 구성되며 다음 속성을 갖습니다:
    -   `workflow_name`: 논리적 워크플로 또는 앱의 이름입니다. 예를 들면 "코드 생성" 또는 "고객 서비스"입니다.
    -   `trace_id`: 트레이스의 고유 ID입니다. 전달하지 않으면 자동으로 생성됩니다. `trace_<32_alphanumeric>` 형식이어야 합니다.
    -   `group_id`: 동일한 대화에 속한 여러 트레이스를 연결하는 선택적 그룹 ID입니다. 예를 들어 채팅 스레드 ID를 사용할 수 있습니다.
    -   `disabled`: True이면 트레이스가 기록되지 않습니다.
    -   `metadata`: 트레이스의 선택적 메타데이터입니다.
-   **스팬**은 시작 및 종료 시간이 있는 작업을 나타냅니다. 스팬은 다음 항목을 포함합니다:
    -   `started_at` 및 `ended_at` 타임스탬프
    -   스팬이 속한 트레이스를 나타내는 `trace_id`
    -   이 스팬의 상위 스팬을 가리키는 `parent_id`(있는 경우)
    -   스팬에 관한 정보인 `span_data`. 예를 들어 `AgentSpanData`에는 에이전트에 관한 정보가 포함되고, `GenerationSpanData`에는 LLM 생성에 관한 정보가 포함됩니다.

## 기본 트레이싱 {#default-tracing}

기본적으로 SDK는 다음 항목을 트레이싱합니다:

-   전체 `Runner.{run, run_sync, run_streamed}()`이 `trace()`으로 래핑됩니다.
-   각 러너 호출이 `task_span()`으로 래핑됩니다.
-   각 모델 턴이 `turn_span()`으로 래핑됩니다.
-   에이전트가 실행될 때마다 `agent_span()`로 래핑됩니다
-   LLM 생성은 `generation_span()`로 래핑됩니다
-   각 함수 도구 호출은 `function_span()`으로 래핑됩니다
-   가드레일은 `guardrail_span()`로 래핑됩니다
-   핸드오프는 `handoff_span()`로 래핑됩니다
-   오디오 입력(음성 텍스트 변환)은 `transcription_span()`으로 래핑됩니다
-   오디오 출력(텍스트 음성 변환)은 `speech_span()`로 래핑됩니다
-   SDK는 관련 오디오 스팬을 `speech_group_span()` 아래의 하위 스팬으로 구성할 수 있습니다

기본적으로 트레이스 이름은 리터럴 문자열 `Agent workflow`입니다. `trace`을 사용하는 경우 이 이름을 설정할 수 있으며, [`RunConfig`][agents.run.RunConfig]을 사용하여 이름과 기타 속성을 구성할 수도 있습니다.

더 간결한 계층 구조가 필요하다면 실행에 대한 자동 작업 및 턴 스팬을 비활성화하세요. 에이전트, 생성, 함수, 가드레일, 핸드오프 및 커스텀 스팬은 계속 기록됩니다.

```python
from agents import RunConfig, Runner

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(tracing={"include_task_and_turn_spans": False}),
)
```

또한 트레이스를 다른 대상으로 전송하도록 [커스텀 트레이싱 프로세서](#custom-tracing-processors)를 설정할 수 있습니다. 이 프로세서는 기본 대상을 대체하거나 보조 대상으로 사용할 수 있습니다.

## 장기 실행 워커와 즉시 내보내기 {#long-running-workers-and-immediate-exports}

기본 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]는 몇 초마다 백그라운드에서 트레이스를 내보내며, 메모리 내 큐가 크기 트리거에 도달하면 더 일찍 내보냅니다. 또한 프로세스가 종료될 때 최종 플러시를 수행합니다. Celery, RQ, Dramatiq 또는 FastAPI 백그라운드 작업과 같은 장기 실행 워커에서는 추가 코드 없이도 일반적으로 트레이스가 자동으로 내보내지지만, 각 작업이 끝난 직후 트레이스 대시보드에 표시되지 않을 수 있습니다.

작업 단위가 끝날 때 즉시 전달되는 것을 보장해야 한다면 트레이스 컨텍스트가 종료된 후 [`flush_traces()`][agents.tracing.flush_traces]을 호출하세요.

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

[`flush_traces()`][agents.tracing.flush_traces]은 현재 버퍼링된 트레이스와 스팬을 모두 내보낼 때까지 블로킹하므로, 부분적으로 생성된 트레이스를 플러시하지 않도록 `trace()`가 닫힌 후 호출하세요. 기본 내보내기 지연 시간을 허용할 수 있다면 이 호출을 생략할 수 있습니다.

트레이싱을 비활성화하면 기본 공급자가 새 트레이스와 스팬을 생성하지 않지만, 해당 프로세서가 이미 버퍼링한 데이터는 삭제되지 않습니다. `set_tracing_disabled(True)` 또는 `OPENAI_AGENTS_DISABLE_TRACING=1`을 통해 트레이싱을 비활성화한 후에도 [`flush_traces()`][agents.tracing.flush_traces]은 버퍼링된 데이터를 계속 플러시합니다.

## 상위 수준 트레이스 {#higher-level-traces}

여러 `run()` 호출을 하나의 트레이스에 포함하고 싶을 때가 있습니다. 전체 코드를 `trace()`으로 래핑하면 됩니다.

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

1. 두 번의 `Runner.run` 호출이 `with trace()`으로 래핑되므로, 각각 별도의 트레이스를 생성하는 대신 두 실행이 하나의 전체 트레이스에 포함됩니다.

## 트레이스 생성 {#creating-traces}

[`trace()`][agents.tracing.trace] 함수를 사용하여 트레이스를 생성할 수 있습니다. 트레이스는 시작하고 종료해야 합니다. 이를 수행하는 방법은 두 가지입니다:

1. **권장**: 트레이스를 컨텍스트 관리자로 사용합니다. 즉, `with trace(...) as my_trace`을 사용합니다. 그러면 적절한 시점에 트레이스가 자동으로 시작되고 종료됩니다.
2. [`trace.start()`][agents.tracing.Trace.start] 및 [`trace.finish()`][agents.tracing.Trace.finish]를 수동으로 호출할 수도 있습니다.

현재 트레이스는 Python [`contextvar`](https://docs.python.org/3/library/contextvars.html)를 통해 추적됩니다. 따라서 동시성 환경에서도 자동으로 작동합니다. 트레이스를 수동으로 시작하고 종료하는 경우 현재 트레이스를 업데이트하려면 `start()`에 `mark_as_current`을 전달하고, `finish()`에 `reset_current`을 전달하세요.

## 스팬 생성 {#creating-spans}

다양한 [`*_span()`][agents.tracing.create] 메서드를 사용하여 스팬을 생성할 수 있습니다. 일반적으로 스팬을 수동으로 생성할 필요는 없습니다. 커스텀 스팬 정보를 추적할 수 있도록 [`custom_span()`][agents.tracing.custom_span] 함수가 제공됩니다.

스팬은 자동으로 현재 트레이스의 일부가 되며, Python [`contextvar`](https://docs.python.org/3/library/contextvars.html)를 통해 추적되는 가장 가까운 현재 스팬 아래에 중첩됩니다.

## 민감한 데이터 {#sensitive-data}

일부 스팬은 잠재적으로 민감한 데이터를 캡처할 수 있습니다.

`generation_span()`에는 LLM 생성의 입력/출력이 저장되고, `function_span()`에는 함수 호출의 입력/출력이 저장됩니다. 이러한 데이터에는 민감한 정보가 포함될 수 있으므로 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]을 통해 데이터 캡처를 비활성화할 수 있습니다.

승인이 필요한 함수 도구의 경우, 승인을 기다리며 일시 중지된 스팬은 SDK의 내부 결과 래퍼를 도구 출력으로 저장하지 않습니다. 애플리케이션이 커스텀 거부 메시지와 함께 호출을 거부하면, 함수 스팬은 `trace_include_sensitive_data`이 `True`일 때만 해당 메시지를 출력 및 오류 텍스트로 저장합니다. 설정이 `False`이면 스팬은 출력을 생략하고 일반 오류 텍스트 `Tool execution rejected`을 사용합니다.

마찬가지로 오디오 스팬에는 기본적으로 입력 및 출력 오디오의 base64 인코딩 PCM 데이터가 포함됩니다. [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]을 구성하여 이 오디오 데이터의 캡처를 비활성화할 수 있습니다.

기본적으로 `trace_include_sensitive_data`은 `True`입니다. 앱을 실행하기 전에 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 환경 변수를 `true/1` 또는 `false/0`로 내보내면 코드 없이 기본값을 설정할 수 있습니다.

`trace_include_sensitive_data`이 `False`이면 Responses 모델 스팬은 요청 입력과 응답 출력을 생략합니다. 공식 OpenAI 엔드포인트 호출의 경우에도 스팬에는 상관관계 메타데이터로 Responses API `response_id`이 포함됩니다. SDK는 커스텀 엔드포인트에서 수정된 스팬에 해당 식별자를 포함하지 않습니다.

## 커스텀 트레이싱 프로세서 {#custom-tracing-processors}

트레이싱의 상위 수준 아키텍처는 다음과 같습니다:

-   초기화 시 트레이스 생성을 담당하는 전역 [`TraceProvider`][agents.tracing.provider.TraceProvider]를 생성합니다.
-   트레이스/스팬을 배치 단위로 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]에 보내는 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]로 `TraceProvider`을 구성합니다. 이 익스포터는 스팬과 트레이스를 OpenAI 백엔드로 일괄 내보냅니다.

트레이스를 다른 백엔드나 추가 백엔드로 보내거나 익스포터 동작을 변경하는 등 기본 설정을 커스터마이즈하는 방법은 두 가지입니다:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]을 사용하면 준비되는 대로 트레이스와 스팬을 수신하는 **추가** 트레이스 프로세서를 등록할 수 있습니다. 이를 통해 OpenAI 백엔드로 트레이스를 보내는 동시에 자체 처리를 수행할 수 있습니다.
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]을 사용하면 기본 프로세서를 자체 트레이스 프로세서로 **대체**할 수 있습니다. 트레이스를 전송하는 `TracingProcessor`을 포함하지 않으면 트레이스가 OpenAI 백엔드로 전송되지 않습니다.


## OpenAI 이외 모델을 사용한 트레이싱 {#tracing-with-non-openai-models}

OpenAI 이외의 모델을 사용할 때 트레이싱을 비활성화하지 않고 OpenAI 트레이스 대시보드에서 무료 트레이싱을 사용하려면 트레이싱 익스포터에 OpenAI API 키를 제공할 수 있습니다. 어댑터 선택 및 설정 시 주의 사항은 모델 가이드의 [서드파티 어댑터](models/index.md#third-party-adapters) 섹션을 참조하세요.

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

단일 실행에만 다른 트레이싱 키가 필요하다면 전역 익스포터를 변경하는 대신 `RunConfig`을 통해 전달하세요.

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

### 외부 트레이싱 프로세서 목록 {#external-tracing-processors-list}

-   [Weights & Biases](https://docs.wandb.ai/weave/guides/integrations/agents/openai-agents-sdk)
-   [Arize Phoenix](https://arize.com/docs/phoenix/integrations/llm-providers/openai/openai-agents-sdk-tracing)
-   [Future AGI](https://docs.futureagi.com/docs/tracing/auto/openai_agents/)
-   [MLflow (셀프 호스티드/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow (Databricks 호스티드)](https://docs.databricks.com/aws/en/mlflow3/genai/tracing/integrations/openai-agent)
-   [Braintrust](https://www.braintrust.dev/docs/integrations/agent-frameworks/openai-agents-sdk)
-   [Pydantic Logfire](https://pydantic.dev/docs/logfire/integrations/llms/openai/#openai-agents)
-   [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
-   [Scorecard](https://docs.scorecard.io/features/tracing#agent-frameworks)
-   [Respan](https://www.respan.ai/docs/integrations/openai-agents-sdk)
-   [LangSmith](https://docs.langchain.com/langsmith/trace-openai)
-   [Maxim AI](https://www.getmaxim.ai/docs/sdk/python/integrations/openai/agents-sdk)
-   [Comet Opik](https://www.comet.com/docs/opik/integrations/openai_agents)
-   [Langfuse](https://langfuse.com/integrations/frameworks/openai-agents)
-   [Langtrace](https://docs.langtrace.ai/supported-integrations/llm-frameworks/openai-agents-sdk)
-   [Okahu-Monocle](https://github.com/monocle2ai/monocle)
-   [Galileo](https://docs.galileo.ai/how-to-guides/third-party-integrations/openai-agent-integration)
-   [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)
-   [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk/)
-   [Agenta](https://agenta.ai/docs/observability/integrations/openai-agents)
-   [PostHog](https://posthog.com/docs/ai-observability/installation/openai-agents)
-   [Traccia](https://traccia.ai/docs/integrations/openai-agents/)
-   [PromptLayer](https://docs.promptlayer.com/features/observability/traces/integrations#openai-agents-sdk)
-   [HoneyHive](https://docs.honeyhive.ai/v2/integrations/openai-agents)
-   [Asqav](https://www.asqav.com/docs/integrations#openai-agents)
-   [Datadog](https://docs.datadoghq.com/llm_observability/instrumentation/auto_instrumentation/?tab=python#openai-agents)
-   [Latitude](https://docs.latitude.so/telemetry/frameworks/openai-agents)
-   [DProvenanceKit](https://dprovenance.dev/openai-agents/)
-   [Tuning Engines](https://github.com/cerebrixos-org/tuning-engines-cli/tree/main/packages/tuning-agents#openai-agents-sdk)