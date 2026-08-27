---
search:
  exclude: true
---
# 릴리스 프로세스/변경 로그

이 프로젝트는 `0.Y.Z` 형식을 사용하는, 약간 수정된 시맨틱 버저닝을 따릅니다. 앞의 `0`은 SDK가 여전히 빠르게 발전하고 있음을 나타냅니다. 각 구성 요소는 다음과 같이 증가합니다.

## 마이너(`Y`) 버전 {#minor-y-versions}

베타로 표시되지 않은 공개 인터페이스에 **호환성을 깨는 변경 사항**이 있을 때 마이너 버전 `Y`을 증가시킵니다. 예를 들어 `0.0.x`에서 `0.1.x`로 변경될 때 호환성을 깨는 변경 사항이 포함될 수 있습니다.

호환성을 깨는 변경 사항을 원하지 않는다면 프로젝트에서 `0.0.x` 버전으로 고정하는 것이 좋습니다.

## 패치(`Z`) 버전 {#patch-z-versions}

호환성을 깨지 않는 다음 변경 사항에는 `Z`을 증가시킵니다.

-   버그 수정
-   새로운 기능
-   비공개 인터페이스 변경
-   베타 기능 업데이트

## 호환성을 깨는 변경 사항 로그 {#breaking-change-changelog}

### 0.22.0 {#0220}

버전 0.22.0에서는 여러 기존 API의 실패 처리와 데이터 격리가 강화되었습니다. 명시적 클라이언트로 `OpenAIProvider`을 생성하면서 프로바이더에도 `organization` 또는 `project`을 전달하는 애플리케이션은 중복 인수를 제거해야 합니다.

주요 변경 사항:

-   에이전트 수준 출력 가드레일이 종결 함수 도구에서 직접 생성된 최종 출력을 차단하면, SDK는 검증된 필드로 안전하게 재구성할 수 있는 경우에만 재실행에 유효한 호출/출력 쌍을 유지합니다. 원래 `function_call_output` 페이로드는 세션 기록, `RunState`, 스트리밍된 결과 상태에서 고정 텍스트 `"Output withheld by an output guardrail."`으로 대체되며, 페이로드가 포함된 현재 응답의 가드레일 메타데이터는 제거되거나 대체됩니다. 현재 응답에 추론 또는 지원되지 않는 다른 형태가 포함되어 있으면 SDK는 대신 현재 응답의 접미부 전체를 폐기합니다. 이전에 수락된 턴과 가드레일 결과는 계속 사용할 수 있습니다. [출력 가드레일](guardrails.md#output-guardrails)을 참고하세요.
-   이제 비스트리밍 OpenAI Responses 호출은 반환된 응답의 최종 상태가 `failed` 또는 `incomplete`이면 기존의 스트리밍 최종 이벤트 처리와 동일하게 `ModelBehaviorError`을 발생시킵니다. 이는 `OpenAIResponsesModel`과 `AnyLLMModel`의 Responses 경로에 적용됩니다. [예외](running_agents.md#exceptions)를 참고하세요.
-   이제 [`OpenAIProvider`][agents.models.openai_provider.OpenAIProvider]은 `openai_client`가 `organization` 또는 `project`와 함께 사용될 때도 `UserError`을 발생시킵니다. `api_key`, `base_url`, `websocket_base_url`과의 기존 충돌은 변경되지 않습니다. 이러한 값은 명시적 `AsyncOpenAI` 클라이언트에 구성하세요. [API 키와 클라이언트](config.md#api-keys-and-clients)를 참고하세요.
-   이제 각 `RunResult.to_state()` 체크포인트는 독립적인 사용량 스냅샷을 소유합니다. 재개된 결과는 체크포인트 합계로 시작하고 자체 모델 호출을 추가하며, 원본 결과나 다른 체크포인트를 변경하지 않습니다. 중첩된 `Agent.as_tool()` 재개는 재개 이후의 사용량을 활성 외부 실행에 계속 집계합니다. [RunState 체크포인트의 사용량](usage.md#usage-in-runstate-checkpoints)을 참고하세요.
-   이제 에이전트 시각화는 `handoff(agent)`로 등록된 대상의 도구, MCP 서버, 이후 핸드오프를 재귀적으로 확장하며, 이는 에이전트의 `handoffs` 목록에 있는 직접적인 `Agent` 항목과 동일합니다. [그래프 생성](visualization.md#generating-a-graph)을 참고하세요.
-   이제 `Agent.clone()` 및 `RealtimeAgent.clone()` API 안내에는 기존의 얕은 복사 동작이 정확히 명시되어 있습니다. 재정의되지 않은 목록 속성은 동일한 목록 객체로 유지됩니다. 복제본이 컨테이너를 독립적으로 소유해야 한다면 새 목록을 전달하세요. [에이전트 복제/복사](agents.md#cloningcopying-agents)를 참고하세요.

### 0.21.0 {#0210}

버전 0.21.0에는 `openai` v3가 필요하며, Agents SDK의 OpenAI HTTP 통합이 HTTPX2로 이전되었습니다. 기본 OpenAI 클라이언트를 사용하는 애플리케이션은 클라이언트 설정을 변경할 필요가 없지만, OpenAI HTTP 계층을 사용자 지정하는 애플리케이션은 전송 계층 관련 코드를 마이그레이션해야 할 수 있습니다.

주요 변경 사항:

-   이제 필수 OpenAI 의존성은 `openai>=3.0.0,<4`입니다. 코어를 새로 설치하면 HTTPX2를 사용하며, 더 이상 레거시 `httpx`을 직접 의존성으로 설치하지 않습니다.
-   이제 기본 OpenAI 프로바이더, Voice 프로바이더, Responses WebSocket 지원, 트레이싱 익스포터, 프로바이더 재시도 정규화는 HTTPX2를 사용합니다. 기존 Agents SDK의 공개 구성과 런타임 동작은 변경되지 않습니다.
-   `AsyncOpenAI`에 `http_client=`을 전달하는 애플리케이션은 사용자 지정 클라이언트, 전송, 인증, 이벤트 훅, 모의 전송, 타임아웃 값, URL, 요청, 응답, 전송 예외 처리를 `httpx`에서 `httpx2`로 마이그레이션해야 합니다. 애플리케이션에 OpenAI 클라이언트의 기본값과 사용자 지정 HTTP 옵션이 모두 필요한 경우 OpenAI Python SDK의 `DefaultAsyncHttpx2Client`을 사용하는 것이 좋습니다. [`openai` v3의 사용자 지정 HTTP 클라이언트](config.md#custom-http-clients-with-openai-v3)를 참고하세요.
-   Agents SDK는 임의의 레거시 HTTPX 객체를 HTTPX2로 변환하지 않습니다. OpenAI Python SDK의 임시 레거시 클라이언트 호환성 경로에는 명시적인 `httpx` 설치가 필요하며, 이를 마이그레이션용 연결 경로로 간주해야 합니다.
-   로컬 MCP HTTP 사용자 지정은 설치된 MCP 패키지를 계속 따릅니다. MCP Python SDK v1은 레거시 `httpx`을 제공하고 사용하며, MCP Python SDK v2는 `httpx2`을 사용합니다. 일반적인 MCP 연결에는 애플리케이션 변경이 필요하지 않습니다. [MCP Python SDK v1 및 v2](mcp.md#mcp-python-sdk-v1-and-v2)를 참고하세요.
-   이제 공개된 프로바이더 중립적 테스트 유틸리티는 프로바이더나 프로세스 의존성 없이 에이전트 모델, 샌드박스 세션, Realtime 세션, Voice 파이프라인 워크플로를 지원합니다. 실제 프로바이더 어댑터 또는 통합 경계를 유지해야 하는 경우에 대한 방법과 안내는 [테스트](testing.md)를 참고하세요.

### 0.20.0 {#0200}

버전 0.20.0에는 로컬 MCP HTTP 전송을 사용자 지정하는 애플리케이션에 잠재적으로 호환성을 깨는 MCP 의존성 마이그레이션이 포함됩니다. 또한 에이전트 또는 실행에서 모델을 명시적으로 선택하지 않을 때 사용하는 SDK 기본 모델이 업데이트되었습니다.

주요 변경 사항:

-   이제 SDK 기본 모델은 `gpt-5.4-mini` 대신 `gpt-5.6-luna`입니다. 기본 `reasoning.effort="none"` 및 `verbosity="low"` 설정은 변경되지 않습니다.
-   명시적인 에이전트 모델, 실행 수준 모델 재정의, `OPENAI_DEFAULT_MODEL` 환경 변수는 계속 SDK 기본값보다 우선합니다.
-   이제 Realtime 입력 전사 설정은 `gpt-transcribe`, `gpt-live-transcribe`, `gpt-realtime-whisper`를 인식합니다. 지연 시간이 짧은 `gpt-live-transcribe` 세션에서는 중첩된 `audio.input.transcription` 설정을 통해 `prompt`, `keywords`, 여러 개의 예상 `languages`을 제공할 수 있습니다. 이 SDK에서 고정한 OpenAI 클라이언트 버전은 `delay` 지연 시간/정확도 수준을 `gpt-realtime-whisper`에서만 지원합니다. 커밋된 오디오 턴 이후의 전사 또는 감지된 언어 출력을 위해서는 WebSocket에서 `gpt-transcribe`을 사용하세요. `audio.input.turn_detection=None`을 명시적으로 설정하면 자동 턴 감지가 비활성화됩니다. [입력 전사 설정](realtime/guide.md#input-transcription-settings)을 참고하세요.
-   이제 Agents SDK에서 생성한 로컬 MCP 연결은 `mcp>=1.19.0,<3`을 통해 v1 호환성을 유지하면서 MCP Python SDK v2를 지원합니다. Agents SDK는 일반적인 stdio, SSE, Streamable HTTP 연결을 자동으로 조정합니다. MCP v2가 설치된 경우 이러한 연결은 `mcp.Client(mode="auto")`을 사용해 지원되는 최신 프로토콜을 탐색하고, 이전 서버에서는 레거시 `initialize` 핸드셰이크로 대체합니다. 의존성 해석에서 MCP v2가 선택되면 사용자 지정 `httpx.Auth` 객체 또는 `httpx.AsyncClient` 팩토리를 제공하는 애플리케이션은 해당 값을 `httpx2`으로 마이그레이션하거나, v1 HTTP 스택을 유지하도록 `mcp<2`을 고정해야 합니다. `MCPServerStreamableHttp`의 `params["ignore_initialized_notification_failure"] = True` 옵션도 계속 v1에서만 사용할 수 있습니다. 마이그레이션에 대한 자세한 내용은 [MCP Python SDK v1 및 v2](mcp.md#mcp-python-sdk-v1-and-v2)를 참고하세요.
-   이제 샌드박스 마운트 검증은 샌드박스 또는 마운트 도우미의 부작용이 발생하기 전에 안전하지 않은 자격 증명 배치를 거부합니다. 신뢰할 수 있는 애플리케이션은 스토리지 기능 테이블을 변경하지 않고도 정확한 컨테이너 내부 마운트 경로에 대해 마운트 범위 또는 광범위한 자격 증명 노출을 확인할 수 있습니다. 이러한 확인은 런타임에만 적용되며, 직렬화된 샌드박스 상태 자체는 자격 증명 권한을 부여하지 않습니다. 보호된 마운트 경계에서 SDK는 새로 편집된 예외를 반환합니다. 원본 예외가 정확히 인식되는 SDK 샌드박스 오류이고 승인된 구조화 필드가 검증되면, 대체 예외는 해당 하위 유형과 검증된 안전한 필드를 유지합니다. 인식된 `MountConfigError`은 SDK가 생성한 안전한 검증 메시지도 유지할 수 있습니다. 그 외에는 SDK가 새로 편집된 일반 오류를 반환합니다. 프로바이더가 제어하거나 승인되지 않은 메시지, 명령 데이터, 참고 사항, 컨텍스트, 원인, 원본 트레이스백 상태는 유지되지 않습니다. [마운트 및 원격 스토리지](sandbox/clients.md#mounts-and-remote-storage)와 [세션 상태에서 재개](sandbox/guide.md#resume-from-session-state)를 참고하세요.
-   재시도 정책은 안정적인 재실행 안전성 정보를 검사하고, 프로바이더가 안전하지 않다고 표시한 비스트리밍 요청에 대해 `RetryDecision(approve_unsafe_replay=True)`을 명시적으로 설정할 수 있습니다. 이 승인은 중단, 이미 내보낸 스트리밍 출력 또는 Programmatic Tool Calling과 같은 별도의 로컬 부작용 거부를 우회하지 않습니다. [Runner 관리형 재시도](models/index.md#runner-managed-retries)를 참고하세요.
-   이제 재개 가능한 `RunState` 객체는 다음 모델 호출 전에 `add_input()`을 사용해 영구 사용자 입력을 스테이징할 수 있습니다. 스테이징된 입력은 직렬화 후에도 유지되고 입력 가드레일을 통과하며, 로컬 세션과 서버 관리형 대화 전체에서 하나의 영구적인 SDK 입력 발생 기록을 생성합니다. 안전하지 않은 재실행을 명시적으로 승인하면 입력이 프로바이더에 다시 전송되고 프로바이더 측 작업이 반복될 수 있습니다. [재개 전 입력 추가](results.md#add-input-before-resuming)를 참고하세요.
-   런타임 안정성 수정으로 스트리밍 및 비스트리밍 [출력 가드레일 세션 영속성](guardrails.md#output-guardrails)이 일치하고, 복사 및 네임스페이스 지정 중에 `FunctionTool` 하위 클래스가 보존되며, 지원되지 않는 [Chat Completions 오디오 출력](models/index.md#chat-completions-compatibility-options)에 대해 빈 스트림을 조용히 완료하는 대신 명시적 오류가 발생합니다. `OpenAIResponsesCompactionSession` 래퍼는 취소가 호출자에게 전달되기 전에 [압축 전 기록 복구](sessions/index.md#auto-compaction-can-block-streaming)를 시도하고 완료될 때까지 기다립니다. 이제 [`VoicePipeline`](voice/pipeline.md#results) 소비자는 실행이 정상적으로 끝난 후 전사 세션 종료 실패를 수신하며, 이전 턴의 실패가 이후 종료 실패보다 우선합니다. 이제 `RunState` 왕복 변환은 로컬 셸 출력, 확인된 컴퓨터 안전 검사, 기본값이 설정된 도구 출력 필드, 딕셔너리·목록·튜플을 순회하는 중 발견한 Pydantic 모델 또는 데이터클래스 출력을 보존합니다. MCP 변환은 자유 형식 객체 스키마와 이미지 출력을 보존하며, 오디오 및 리소스 블록과 같은 기타 raw 콘텐츠 블록을 유효한 JSON 텍스트로 직렬화합니다. `MCPServerManager`는 겹치는 수명 주기 작업을 직렬화하고 연결 및 정리에 유한한 기본 타임아웃을 적용합니다. 모델 재실행은 출력 항목을 입력으로 사용하기 전에 서버 소유 `created_by` 메타데이터를 제거합니다.

### 0.19.0 {#0190}

이 마이너 릴리스에는 호환성을 깨는 변경 사항이 **없습니다**. 마이너 버전 증가는 OpenAI Responses의 중요한 새 기능 영역인 Programmatic Tool Calling을 반영합니다.

주요 변경 사항:

-   지원되는 OpenAI Responses 모델이 Programmatic Tool Calling에 적합한 도구를 조정하기 위한 JavaScript를 생성할 수 있게 해주는 [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool]이 추가되었습니다. 도구별 `allowed_callers`, `FunctionTool` 인스턴스의 structured outputs, Runner 스트리밍, 가드레일, 승인, 세션, `RunState`과의 통합을 지원합니다. 설정 및 제약 조건은 [Programmatic Tool Calling](tools.md#programmatic-tool-calling)을 참고하세요.
-   공개 `agents.decorators` 모듈과 기존 `@function_tool` 데코레이터의 짧은 별칭인 `@tool`이 기존 가드레일 데코레이터와 함께 추가되었습니다. 이제 `FunctionTool` 인스턴스는 비동기 호출 가능 객체도 지원합니다.
-   이제 SDK 구성은 에이전트, 실행, 모델, 세션, 샌드박스, Voice 파이프라인 전반에서 타입이 지정된 설정 객체 또는 딕셔너리를 일관되게 허용하며, 알 수 없는 설정을 검증합니다.
-   유용한 디버깅 컨텍스트를 유지하면서 가공되지 않은 민감한 페이로드가 노출되지 않도록 모델, 도구, MCP, Realtime, 세션, 샌드박스, 트레이싱 전반의 오류 및 진단 로깅이 강화되었습니다.
-   AnyLLM, LiteLLM, Chat Completions 호환성이 개선되고 모델 재시도 전반에서 세션 기록이 보존되며, 응답이 시작되기 전에 발생하는 WebSocket 과부하에 대한 프로바이더 재시도 안내가 추가되었습니다. 이에 따라 허용되는 경우 명시적으로 활성화한 Runner 재시도 정책이 실패한 시도를 재실행할 수 있습니다.
-   `VercelCloudBucketMountStrategy`을 통해 [Vercel 샌드박스를 생성할 때만 구성할 수 있는 S3 마운트](sandbox/clients.md#mounts-and-remote-storage)가 추가되었습니다. 마운트된 세션은 워크스페이스 영속성에서 버킷 콘텐츠를 제외하며, 의도적으로 동적 마운트 변경이나 세션 재개를 지원하지 않습니다.

### 0.18.0 {#0180}

이 마이너 릴리스에는 호환성을 깨는 변경 사항이 **없습니다**. 마이너 버전 증가는 Realtime 에이전트의 기본 모델 업데이트만을 위한 것입니다.

주요 변경 사항:

-   이제 Realtime 에이전트는 `gpt-realtime-2.1`을 기본 모델로 사용하므로, 새로운 Realtime 설정에서는 별도 구성 없이 최신 권장 모델을 사용합니다.

### 0.17.0 {#0170}

이 버전에서 샌드박스 로컬 소스 구체화는 소스 경로가 `Manifest.extra_path_grants`의 적용을 받지 않는 한 `LocalFile.src` 및 `LocalDir.src`을 구체화 `base_dir` 내부로 제한합니다. `base_dir`은 매니페스트가 적용될 때 SDK 프로세스의 현재 작업 디렉터리입니다. 상대 로컬 소스는 해당 디렉터리를 기준으로 해석되며, 절대 로컬 소스는 이미 그 내부에 있거나 명시적 허용 범위 아래에 있어야 합니다. 이 변경은 로컬 아티팩트 경계 문제를 해결하지만, 신뢰할 수 있는 호스트 파일이나 디렉터리를 해당 기본 디렉터리 외부에서 샌드박스 워크스페이스로 의도적으로 복사하는 애플리케이션에 영향을 줄 수 있습니다.

마이그레이션하려면 매니페스트 수준에서 `SandboxPathGrant`을 사용해 신뢰할 수 있는 호스트 루트를 허용하세요. 샌드박스가 해당 파일을 읽기만 하면 되는 경우 읽기 전용으로 설정하는 것이 좋습니다.

```python
from pathlib import Path

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.entries import Dir, LocalDir

# This is an absolute host path outside the SDK process base_dir.
TRUSTED_DOCS_ROOT = Path("/opt/my-app/docs")

manifest = Manifest(
    extra_path_grants=(
        # This host root is outside the SDK process base_dir, so the manifest must grant it.
        SandboxPathGrant(path=str(TRUSTED_DOCS_ROOT), read_only=True),
    ),
    entries={
        # No grant is needed for local sources that stay under the SDK process base_dir.
        "fixtures": LocalDir(src=Path("fixtures"), description="Local test fixtures."),
        # This entry reads from the granted host root and copies it into the sandbox workspace.
        "docs": LocalDir(src=TRUSTED_DOCS_ROOT, description="Trusted local documents."),
        # Dir creates a sandbox workspace directory; it does not read from the host filesystem.
        "output": Dir(description="Generated artifacts."),
    },
)
```

`extra_path_grants`을 신뢰할 수 있는 애플리케이션 구성으로 취급하세요. 애플리케이션에서 해당 호스트 경로를 이미 승인하지 않았다면 모델 출력이나 기타 신뢰할 수 없는 매니페스트 입력으로 허용 범위를 채우지 마세요.

### 0.16.0 {#0160}

이 버전에서 SDK 기본 모델은 `gpt-4.1` 대신 `gpt-5.4-mini`입니다. 이는 모델을 명시적으로 설정하지 않은 에이전트와 실행에 영향을 줍니다. 새로운 기본값은 GPT-5 모델이므로 암시적 기본 모델 설정에는 이제 `reasoning.effort="none"` 및 `verbosity="low"`와 같은 GPT-5 기본값이 포함됩니다.

이전 기본 모델 동작을 유지해야 한다면 에이전트 또는 실행 구성에 모델을 명시적으로 설정하거나 `OPENAI_DEFAULT_MODEL` 환경 변수를 설정하세요.

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

주요 변경 사항:

-   이제 `Runner.run`, `Runner.run_sync`, `Runner.run_streamed`은 턴 제한을 비활성화하는 `max_turns=None`을 허용합니다.
-   이제 로컬, Docker, 프로바이더 기반 샌드박스 구현 전체에서 샌드박스 워크스페이스 하이드레이션은 절대 심볼릭 링크 대상을 포함해 아카이브 루트 외부를 가리키는 심볼릭 링크가 있는 tar 아카이브를 거부합니다.

### 0.15.0 {#0150}

이 버전에서는 이제 모델 거부가 빈 텍스트 출력으로 처리되거나 structured outputs의 경우 실행 루프가 `MaxTurnsExceeded`까지 재시도하게 하는 대신 `ModelRefusalError`으로 명시적으로 노출됩니다.

이는 이전에 거부만 포함된 모델 응답이 `final_output == ""`으로 완료될 것으로 예상했던 코드에 영향을 줍니다. 예외를 발생시키지 않고 거부를 처리하려면 `model_refusal` 실행 오류 핸들러를 제공하세요.

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

structured outputs 에이전트의 경우 핸들러가 에이전트의 출력 스키마과 일치하는 값을 반환할 수 있으며, SDK는 이를 다른 실행 오류 핸들러의 최종 출력과 동일하게 검증합니다.

### 0.14.0 {#0140}

이 마이너 릴리스에는 호환성을 깨는 변경 사항이 **없지만**, 주요한 새 베타 기능 영역인 샌드박스 에이전트와 로컬, 컨테이너화 및 호스팅 환경 전반에서 이를 사용하는 데 필요한 런타임, 백엔드, 문서 지원이 추가되었습니다.

주요 변경 사항:

-   `SandboxAgent`, `Manifest`, `SandboxRunConfig`을 중심으로 한 새로운 베타 샌드박스 런타임 인터페이스가 추가되어, 에이전트가 파일, 디렉터리, Git 저장소, 마운트, 스냅샷, 재개 지원을 갖춘 영구 격리 워크스페이스 내에서 작업할 수 있습니다.
-   `UnixLocalSandboxClient` 및 `DockerSandboxClient`을 통한 로컬 및 컨테이너화 개발용 샌드박스 실행 백엔드와 Python 패키지의 선택적 의존성 extras를 통한 Blaxel, Cloudflare, Daytona, E2B, Modal, Runloop, Vercel 호스팅 프로바이더 통합이 추가되었습니다.
-   향후 실행에서 이전 실행의 학습 내용을 재사용할 수 있도록 샌드박스 메모리 지원이 추가되었습니다. 여기에는 점진적 공개, 다중 턴 그룹화, 구성 가능한 격리 경계, S3 기반 워크플로를 포함한 영구 메모리 예제가 포함됩니다.
-   로컬 및 합성 워크스페이스 항목, S3/R2/GCS/Azure Blob Storage/S3 Files용 원격 스토리지 마운트, 이식 가능한 스냅샷, `RunState`, `SandboxSessionState` 또는 저장된 스냅샷을 통한 재개 흐름을 포함하는 더 광범위한 워크스페이스 및 재개 모델이 추가되었습니다.
-   `examples/sandbox/` 아래에 스킬, 핸드오프, 메모리, 프로바이더별 설정을 활용한 코딩 작업과 코드 검토, 데이터룸 QA, 웹사이트 복제 같은 엔드투엔드 워크플로를 다루는 다양한 샌드박스 코드 예제 및 튜토리얼이 추가되었습니다.
-   샌드박스를 인식하는 세션 준비, 기능 바인딩, 상태 직렬화, 통합 트레이싱, 프롬프트 캐시 키 기본값, 더 안전한 민감한 MCP 출력 편집을 통해 코어 런타임과 트레이싱 스택이 확장되었습니다.

### 0.13.0 {#0130}

이 마이너 릴리스에는 호환성을 깨는 변경 사항이 **없지만**, 주목할 만한 Realtime 기본값 업데이트와 새로운 MCP 기능 및 런타임 안정성 수정이 포함됩니다.

주요 변경 사항:

-   이제 기본 websocket Realtime 모델은 `gpt-realtime-1.5`이므로, 새로운 Realtime 에이전트 설정에서는 별도 구성 없이 최신 모델을 사용합니다.
-   이제 `MCPServer`은 `list_resources()`, `list_resource_templates()`, `read_resource()`을 노출하고, `MCPServerStreamableHttp`은 `session_id`을 노출하므로 MCP Streamable HTTP 전송을 사용하는 세션을 재연결 또는 상태 비저장 워커 간에 재개할 수 있습니다.
-   이제 Chat Completions 통합은 `should_replay_reasoning_content`을 통해 기존 추론 콘텐츠를 다시 전송하도록 선택할 수 있어 LiteLLM/DeepSeek 같은 어댑터의 프로바이더별 추론/도구 호출 연속성이 향상됩니다.
-   `SQLAlchemySession`의 동시 최초 쓰기, 추론 제거 후 고립된 어시스턴트 메시지 ID가 포함된 압축 요청, MCP/추론 항목을 남기는 `remove_all_tools()`, `FunctionTool` 인스턴스용 배치 실행기의 경쟁 상태를 포함한 여러 런타임 및 세션 경계 사례가 수정되었습니다.

### 0.12.0 {#0120}

이 마이너 릴리스에는 호환성을 깨는 변경 사항이 **없습니다**. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)를 확인하세요.

### 0.11.0 {#0110}

이 마이너 릴리스에는 호환성을 깨는 변경 사항이 **없습니다**. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)를 확인하세요.

### 0.10.0 {#0100}

이 마이너 릴리스에는 호환성을 깨는 변경 사항이 **없지만**, OpenAI Responses 사용자를 위한 중요한 새 기능 영역인 Responses API의 websocket 전송 지원이 포함됩니다.

주요 변경 사항:

-   OpenAI Responses 모델에 대한 websocket 전송 지원이 추가되었습니다(선택 사항이며 HTTP가 계속 기본 전송입니다).
-   여러 턴의 실행에서 공유 websocket 지원 프로바이더와 `RunConfig`을 재사용하기 위한 `responses_websocket_session()` 도우미 / `ResponsesWebSocketSession`가 추가되었습니다.
-   스트리밍, 도구, 승인, 후속 턴을 다루는 새로운 websocket 스트리밍 예제(`examples/basic/stream_ws.py`)가 추가되었습니다.

### 0.9.0 {#090}

이 버전에서는 주요 버전의 지원 종료(EOL) 후 3개월이 지났으므로 Python 3.9를 더 이상 지원하지 않습니다. 더 최신 런타임 버전으로 업그레이드하세요.

또한 `Agent#as_tool()` 메서드에서 반환되는 값의 타입 힌트가 `Tool`에서 `FunctionTool`으로 좁혀졌습니다. 이 변경은 일반적으로 호환성을 깨는 문제를 일으키지 않지만, 코드가 더 넓은 유니온 타입에 의존하는 경우 일부 조정이 필요할 수 있습니다.

### 0.8.0 {#080}

이 버전에서는 두 가지 런타임 동작 변경으로 인해 마이그레이션 작업이 필요할 수 있습니다.

- **동기식** Python 호출 가능 객체를 래핑하는 `FunctionTool` 인스턴스는 이제 이벤트 루프 스레드에서 실행되는 대신 `asyncio.to_thread(...)`을 통해 워커 스레드에서 실행됩니다. 도구 로직이 스레드 로컬 상태 또는 특정 스레드에 종속된 리소스에 의존하는 경우 비동기 도구 구현으로 마이그레이션하거나 도구 코드에 스레드 종속성을 명시하세요.
- 이제 로컬 MCP 도구 실패 처리를 구성할 수 있으며, 기본 동작은 전체 실행을 실패시키는 대신 모델에 표시되는 오류 출력을 반환할 수 있습니다. 즉시 실패하는 의미 체계에 의존한다면 `mcp_config={"failure_error_function": None}`을 설정하세요. 서버 수준 `failure_error_function` 값은 에이전트 수준 설정을 재정의하므로 명시적 핸들러가 있는 각 로컬 MCP 서버에 `failure_error_function=None`을 설정하세요.

### 0.7.0 {#070}

이 버전에서는 기존 애플리케이션에 영향을 줄 수 있는 몇 가지 동작 변경이 있었습니다.

- 이제 중첩된 핸드오프 기록은 **명시적으로 활성화해야 합니다**(기본적으로 비활성화됨). v0.6.x의 기본 중첩 동작에 의존했다면 `RunConfig(nest_handoff_history=True)`을 명시적으로 설정하세요.
- `gpt-5.1` / `gpt-5.2`의 기본 `reasoning.effort`이 `"none"`으로 변경되었습니다(SDK 기본값으로 구성된 이전 기본값은 `"low"`). 프롬프트 또는 품질/비용 프로필이 `"low"`에 의존했다면 `model_settings`에서 명시적으로 설정하세요.

### 0.6.0 {#060}

이 버전에서 기본 핸드오프 기록은 사용자와 어시스턴트 턴을 별도 메시지로 전달하는 대신 하나의 어시스턴트 메시지로 패키징되므로 이후 에이전트가 간결하고 예측 가능한 요약을 받습니다
- 기존 단일 메시지 핸드오프 기록은 이제 기본적으로 `<CONVERSATION HISTORY>` 블록 앞에 정확한 리터럴 텍스트 `For context, here is the conversation so far between the user and the previous agent:`으로 시작하므로 이후 에이전트가 명확히 표시된 요약을 받습니다

### 0.5.0 {#050}

이 버전에는 사용자에게 드러나는 호환성을 깨는 변경 사항이 없지만, 내부적으로 새로운 기능과 몇 가지 중요한 업데이트가 포함됩니다.

- `RealtimeRunner`에 [SIP 프로토콜 연결](https://platform.openai.com/docs/guides/realtime-sip) 처리 지원이 추가되었습니다.
- Python 3.14 호환성을 위해 `Runner#run_sync`의 내부 로직이 대폭 수정되었습니다.

### 0.4.0 {#040}

이 버전에서는 [openai](https://pypi.org/project/openai/) 패키지 v1.x 버전을 더 이상 지원하지 않습니다. 이 SDK와 함께 openai v2.x를 사용하세요.

### 0.3.0 {#030}

이 버전에서 Realtime API 지원은 gpt-realtime 모델과 해당 API 인터페이스(GA 버전)로 마이그레이션됩니다.

### 0.2.0 {#020}

이 버전에서는 이전에 `Agent`을 인수로 받던 몇몇 위치가 이제 `AgentBase`을 인수로 받습니다. 예를 들어 MCP 서버의 `list_tools()` 메서드 시그니처가 이에 해당합니다. 이는 순수한 타입 변경이며, 계속 `Agent` 객체를 받게 됩니다. 업데이트하려면 `Agent`을 `AgentBase`로 대체해 타입 오류를 수정하면 됩니다.

### 0.1.0 {#010}

이 버전에서 [`MCPServer.list_tools()`][agents.mcp.server.MCPServer]에는 `run_context` 및 `agent`이라는 두 가지 새로운 매개변수가 추가되었습니다. `MCPServer`의 하위 클래스에서 재정의한 모든 `MCPServer.list_tools()` 메서드에 이 매개변수를 추가해야 합니다.