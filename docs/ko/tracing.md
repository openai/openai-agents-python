---
search:
  exclude: true
---
# 트레이싱

Agents SDK에는 기본 제공 트레이싱이 포함되어 있으며, 에이전트 실행 중 발생한 이벤트의 포괄적인 기록을 수집합니다: LLM 생성, 도구 호출, 핸드오프, 가드레일, 그리고 발생하는 사용자 정의 이벤트까지 포함됩니다. [Traces 대시보드](https://platform.openai.com/traces)를 사용하면 개발 중과 프로덕션에서 워크플로를 디버그, 시각화, 모니터링할 수 있습니다.

!!!note

    트레이싱은 기본적으로 활성화되어 있습니다. 다음의 일반적인 세 가지 방법으로 비활성화할 수 있습니다:

    1. 환경 변수 `OPENAI_AGENTS_DISABLE_TRACING=1`을 설정하여 전역적으로 트레이싱을 비활성화할 수 있습니다
    2. 코드에서 [`set_tracing_disabled(True)`][agents.set_tracing_disabled]를 사용해 전역적으로 트레이싱을 비활성화할 수 있습니다
    3. 단일 실행에 대해 [`agents.run.RunConfig.tracing_disabled`][]를 `True`로 설정해 트레이싱을 비활성화할 수 있습니다

***OpenAI API를 사용하면서 ZDR(Zero Data Retention) 정책으로 운영하는 조직에서는 트레이싱을 사용할 수 없습니다.***

## 트레이스와 스팬

-   **트레이스**는 하나의 "워크플로"에 대한 단일 end-to-end 작업을 나타냅니다. 트레이스는 스팬으로 구성됩니다. 트레이스에는 다음 속성이 있습니다:
    -   `workflow_name`: 논리적 워크플로 또는 앱입니다. 예: "코드 생성" 또는 "고객 서비스"
    -   `trace_id`: 트레이스의 고유 ID입니다. 전달하지 않으면 자동 생성됩니다. 형식은 `trace_<32_alphanumeric>`이어야 합니다
    -   `group_id`: 선택적 그룹 ID로, 같은 대화의 여러 트레이스를 연결합니다. 예를 들어 채팅 스레드 ID를 사용할 수 있습니다
    -   `disabled`: True이면 트레이스가 기록되지 않습니다
    -   `metadata`: 트레이스용 선택적 메타데이터입니다
-   **스팬**은 시작 시간과 종료 시간이 있는 작업을 나타냅니다. 스팬에는 다음이 있습니다:
    -   `started_at` 및 `ended_at` 타임스탬프
    -   소속된 트레이스를 나타내는 `trace_id`
    -   이 스팬의 부모 스팬을 가리키는 `parent_id`(있는 경우)
    -   스팬에 대한 정보인 `span_data` 예: `AgentSpanData`에는 Agent 정보가, `GenerationSpanData`에는 LLM 생성 정보가 포함됩니다

## 기본 트레이싱

기본적으로 SDK는 다음을 트레이싱합니다:

-   전체 `Runner.{run, run_sync, run_streamed}()`는 `trace()`로 래핑됩니다
-   에이전트가 실행될 때마다 `agent_span()`으로 래핑됩니다
-   LLM 생성은 `generation_span()`으로 래핑됩니다
-   함수 도구 호출은 각각 `function_span()`으로 래핑됩니다
-   가드레일은 `guardrail_span()`으로 래핑됩니다
-   핸드오프는 `handoff_span()`으로 래핑됩니다
-   오디오 입력(음성-텍스트)은 `transcription_span()`으로 래핑됩니다
-   오디오 출력(텍스트-음성)은 `speech_span()`으로 래핑됩니다
-   관련 오디오 스팬은 `speech_group_span()` 아래에 부모로 연결될 수 있습니다

기본적으로 트레이스 이름은 "Agent workflow"입니다. `trace`를 사용하면 이 이름을 설정할 수 있고, [`RunConfig`][agents.run.RunConfig]를 사용하면 이름 및 기타 속성을 구성할 수 있습니다.

또한 [사용자 정의 트레이스 프로세서](#custom-tracing-processors)를 설정해 트레이스를 다른 대상으로 전송할 수 있습니다(대체 또는 보조 대상).

## 장기 실행 워커와 즉시 내보내기

기본 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]는 트레이스를
몇 초마다 백그라운드에서 내보내거나, 메모리 내 큐가 크기 트리거에 도달하면 더 빨리 내보내며,
프로세스 종료 시 최종 플러시도 수행합니다. Celery,
RQ, Dramatiq 또는 FastAPI 백그라운드 작업 같은 장기 실행 워커에서는 일반적으로 트레이스가
추가 코드 없이 자동으로 내보내지지만, 각 작업이 끝난 직후에는 Traces 대시보드에 즉시
표시되지 않을 수 있습니다.

작업 단위 종료 시 즉시 전달 보장이 필요하다면
트레이스 컨텍스트를 종료한 후 [`flush_traces()`][agents.tracing.flush_traces]를 호출하세요.

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

[`flush_traces()`][agents.tracing.flush_traces]는 현재 버퍼링된 트레이스와 스팬이
내보내질 때까지 블로킹하므로, 부분적으로 구성된 트레이스를 플러시하지 않도록 `trace()`가
닫힌 뒤 호출하세요. 기본 내보내기 지연 시간이 허용 가능하다면 이 호출은
생략할 수 있습니다.

## 상위 수준 트레이스

때로는 여러 `run()` 호출을 하나의 단일 트레이스에 포함하고 싶을 수 있습니다. 이 경우 전체 코드를 `trace()`로 래핑하면 됩니다.

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

1. 두 번의 `Runner.run` 호출이 `with trace()`로 래핑되어 있으므로, 개별 실행이 각각 두 개의 트레이스를 만드는 대신 전체 트레이스의 일부가 됩니다

## 트레이스 생성

[`trace()`][agents.tracing.trace] 함수를 사용해 트레이스를 생성할 수 있습니다. 트레이스는 시작과 종료가 필요하며, 방법은 두 가지입니다:

1. **권장**: 트레이스를 컨텍스트 매니저로 사용합니다. 즉 `with trace(...) as my_trace` 형태입니다. 이렇게 하면 적절한 시점에 트레이스가 자동으로 시작/종료됩니다
2. [`trace.start()`][agents.tracing.Trace.start]와 [`trace.finish()`][agents.tracing.Trace.finish]를 수동으로 호출할 수도 있습니다

현재 트레이스는 Python [`contextvar`](https://docs.python.org/3/library/contextvars.html)를 통해 추적됩니다. 이는 동시성 환경에서도 자동으로 작동함을 의미합니다. 트레이스를 수동으로 시작/종료하는 경우 현재 트레이스를 갱신하기 위해 `start()`/`finish()`에 `mark_as_current`와 `reset_current`를 전달해야 합니다.

## 스팬 생성

다양한 [`*_span()`][agents.tracing.create] 메서드를 사용해 스팬을 생성할 수 있습니다. 일반적으로는 스팬을 수동으로 만들 필요가 없습니다. 사용자 정의 스팬 정보를 추적하기 위한 [`custom_span()`][agents.tracing.custom_span] 함수도 제공됩니다.

스팬은 자동으로 현재 트레이스의 일부가 되며, Python [`contextvar`](https://docs.python.org/3/library/contextvars.html)로 추적되는 가장 가까운 현재 스팬 아래에 중첩됩니다.

## 민감한 데이터

일부 스팬은 잠재적으로 민감한 데이터를 수집할 수 있습니다.

`generation_span()`은 LLM 생성의 입력/출력을 저장하고, `function_span()`은 함수 호출의 입력/출력을 저장합니다. 여기에는 민감한 데이터가 포함될 수 있으므로 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]를 통해 해당 데이터 수집을 비활성화할 수 있습니다.

마찬가지로 오디오 스팬에는 기본적으로 입력/출력 오디오용 base64 인코딩 PCM 데이터가 포함됩니다. [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]를 구성해 이 오디오 데이터 수집을 비활성화할 수 있습니다.

기본적으로 `trace_include_sensitive_data`는 `True`입니다. 앱 실행 전에 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 환경 변수를 `true/1` 또는 `false/0`으로 내보내 코드 없이 기본값을 설정할 수 있습니다.

## 사용자 정의 트레이싱 프로세서

트레이싱의 상위 수준 아키텍처는 다음과 같습니다:

-   초기화 시 전역 [`TraceProvider`][agents.tracing.setup.TraceProvider]를 생성하며, 이는 트레이스 생성을 담당합니다
-   `TraceProvider`는 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]로 구성되며, 이 프로세서는 트레이스/스팬을 배치로 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]에 전송하고, 해당 exporter는 스팬과 트레이스를 배치로 OpenAI 백엔드에 내보냅니다

기본 구성을 사용자 지정해 대체 또는 추가 백엔드로 트레이스를 전송하거나 exporter 동작을 수정하려면 두 가지 옵션이 있습니다:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]를 사용하면 준비된 트레이스와 스팬을 수신할 **추가** 트레이스 프로세서를 추가할 수 있습니다. 이를 통해 OpenAI 백엔드로 전송하는 것에 더해 자체 처리를 수행할 수 있습니다
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]를 사용하면 기본 프로세서를 사용자 정의 트레이스 프로세서로 **대체**할 수 있습니다. 이 경우 해당 작업을 수행하는 `TracingProcessor`를 포함하지 않으면 트레이스가 OpenAI 백엔드로 전송되지 않습니다


## 비 OpenAI 모델에서의 트레이싱

트레이싱을 비활성화할 필요 없이 OpenAI Traces 대시보드에서 무료 트레이싱을 활성화하기 위해 비 OpenAI 모델과 함께 OpenAI API 키를 사용할 수 있습니다. 어댑터 선택과 설정 시 주의사항은 Models 가이드의 [서드파티 어댑터](models/index.md#third-party-adapters) 섹션을 참고하세요.

```python
import os
from agents import set_tracing_export_api_key, Agent, Runner
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

단일 실행에 대해서만 다른 트레이싱 키가 필요하다면 전역 exporter를 변경하는 대신 `RunConfig`를 통해 전달하세요.

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 추가 참고 사항
- Openai Traces 대시보드에서 무료 트레이스를 확인하세요


## 에코시스템 통합

다음 커뮤니티 및 벤더 통합은 OpenAI Agents SDK 트레이싱 표면을 지원합니다.

### 외부 트레이싱 프로세서 목록

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow (self-hosted/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow (Databricks hosted)](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
-   [Braintrust](https://braintrust.dev/docs/guides/traces/integrations#openai-agents-sdk)
-   [Pydantic Logfire](https://logfire.pydantic.dev/docs/integrations/llms/openai/#openai-agents)
-   [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
-   [Scorecard](https://docs.scorecard.io/docs/documentation/features/tracing#openai-agents-sdk-integration)
-   [Respan](https://respan.ai/docs/integrations/tracing/openai-agents-sdk)
-   [LangSmith](https://docs.smith.langchain.com/observability/how_to_guides/trace_with_openai_agents_sdk)
-   [Maxim AI](https://www.getmaxim.ai/docs/observe/integrations/openai-agents-sdk)
-   [Comet Opik](https://www.comet.com/docs/opik/tracing/integrations/openai_agents)
-   [Langfuse](https://langfuse.com/docs/integrations/openaiagentssdk/openai-agents)
-   [Langtrace](https://docs.langtrace.ai/supported-integrations/llm-frameworks/openai-agents-sdk)
-   [Okahu-Monocle](https://github.com/monocle2ai/monocle)
-   [Galileo](https://v2docs.galileo.ai/integrations/openai-agent-integration#openai-agent-integration)
-   [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)
-   [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk)
-   [Agenta](https://docs.agenta.ai/observability/integrations/openai-agents)
-   [PostHog](https://posthog.com/docs/llm-analytics/installation/openai-agents)
-   [Traccia](https://traccia.ai/docs/integrations/openai-agents)
-   [PromptLayer](https://docs.promptlayer.com/languages/integrations#openai-agents-sdk)
-   [HoneyHive](https://docs.honeyhive.ai/v2/integrations/openai-agents)