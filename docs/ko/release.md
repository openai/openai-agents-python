---
search:
  exclude: true
---
# 릴리스 프로세스/변경 로그

이 프로젝트는 `0.Y.Z` 형식을 사용하는 약간 변형된 시맨틱 버저닝을 따릅니다. 맨 앞의 `0`은 SDK가 여전히 빠르게 발전하고 있음을 나타냅니다. 각 구성 요소는 다음과 같이 증가시킵니다.

## 마이너(`Y`) 버전

베타로 표시되지 않은 공개 인터페이스에 **호환성을 깨는 변경 사항**이 발생하면 마이너 버전 `Y`를 증가시킵니다. 예를 들어 `0.0.x`에서 `0.1.x`로 변경할 때 호환성을 깨는 변경 사항이 포함될 수 있습니다.

호환성을 깨는 변경 사항을 원하지 않는다면 프로젝트에서 `0.0.x` 버전으로 고정하는 것이 좋습니다.

## 패치(`Z`) 버전

하위 호환성을 유지하는 다음 변경 사항에는 `Z`를 증가시킵니다.

-   버그 수정
-   새로운 기능
-   비공개 인터페이스 변경
-   베타 기능 업데이트

## 호환성을 깨는 변경 사항의 변경 로그

### 0.19.0

이번 마이너 릴리스에는 호환성을 깨는 변경 사항이 **포함되지 않습니다**. 마이너 버전 상향은 OpenAI Responses의 주요 신규 기능 영역인 프로그래밍 방식 도구 호출(Programmatic Tool Calling)을 반영합니다.

주요 내용:

-   지원되는 OpenAI Responses 모델이 대상 도구를 조정하는 JavaScript를 생성할 수 있게 해주는 [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool]을 추가했습니다. 도구별 `allowed_callers`, 구조화된 함수 도구 출력, Runner 스트리밍, 가드레일, 승인, 세션 및 `RunState`와의 통합을 지원합니다. 설정 및 제약 조건은 [프로그래밍 방식 도구 호출](tools.md#programmatic-tool-calling)을 참조하세요.
-   공개 `agents.decorators` 모듈과 기존 함수 및 가드레일 데코레이터보다 짧은 `@tool` 별칭을 추가했습니다. 이제 함수 도구는 비동기 호출 가능 객체도 지원합니다.
-   이제 SDK 구성은 에이전트, 실행, 모델, 세션, 샌드박스 및 음성 파이프라인 전반에서 타입이 지정된 설정 객체나 딕셔너리를 일관되게 허용하며, 알 수 없는 설정을 검증합니다.
-   유용한 디버깅 컨텍스트는 유지하면서 민감한 원문 페이로드가 노출되지 않도록 모델, 도구, MCP, Realtime, 세션, 샌드박스 및 트레이싱 전반의 오류 및 진단 로깅을 강화했습니다.
-   AnyLLM, LiteLLM 및 Chat Completions 호환성을 개선하고, 모델 재시도 중에도 세션 기록을 유지하도록 했으며, 응답이 시작되기 전에 발생하는 WebSocket 과부하에 대한 공급자 재시도 지침을 추가했습니다. 이를 통해 요청 재생이 허용되는 경우 명시적으로 활성화한 Runner 재시도 정책이 작동할 수 있습니다.
-   `VercelCloudBucketMountStrategy`를 통해 [Vercel 샌드박스에서 생성 시에만 사용할 수 있는 S3 마운트](sandbox/clients.md#mounts-and-remote-storage)를 추가했습니다. 마운트된 세션에서는 버킷 내용이 워크스페이스 영속화 대상에서 제외되며, 의도적으로 동적 마운트 변경이나 세션 재개를 지원하지 않습니다.

### 0.18.0

이번 마이너 릴리스에는 호환성을 깨는 변경 사항이 **포함되지 않습니다**. 마이너 버전 상향은 Realtime agents의 기본 모델 업데이트만 반영합니다.

주요 내용:

-   이제 Realtime agents는 `gpt-realtime-2.1`을 기본 모델로 사용하므로, 새로운 Realtime 설정에서는 별도의 구성 없이 최신 권장 모델을 사용합니다.

### 0.17.0

이 버전에서는 샌드박스 로컬 소스 구체화 시 소스 경로가 `Manifest.extra_path_grants`의 적용 대상이 아닌 한 `LocalFile.src`와 `LocalDir.src`를 구체화 `base_dir` 내부로 제한합니다. `base_dir`은 매니페스트가 적용될 때 SDK 프로세스의 현재 작업 디렉터리입니다. 상대 경로 로컬 소스는 이 디렉터리를 기준으로 해석되며, 절대 경로 로컬 소스는 이미 이 디렉터리 내부에 있거나 명시적으로 권한이 부여된 경로 아래에 있어야 합니다. 이 변경으로 로컬 아티팩트 경계 문제가 해결되지만, 해당 기본 디렉터리 외부의 신뢰할 수 있는 호스트 파일이나 디렉터리를 샌드박스 워크스페이스로 의도적으로 복사하는 애플리케이션에는 영향을 줄 수 있습니다.

마이그레이션하려면 매니페스트 수준에서 `SandboxPathGrant`를 사용하여 신뢰할 수 있는 호스트 루트에 권한을 부여하세요. 샌드박스에서 해당 파일을 읽기만 하면 되는 경우에는 읽기 전용 권한을 사용하는 것이 좋습니다.

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

`extra_path_grants`는 신뢰할 수 있는 애플리케이션 구성으로 취급하세요. 애플리케이션에서 해당 호스트 경로를 이미 승인한 경우가 아니라면 모델 출력이나 신뢰할 수 없는 다른 매니페스트 입력으로 권한을 채우면 안 됩니다.

### 0.16.0

이 버전에서는 SDK 기본 모델이 `gpt-4.1`에서 `gpt-5.4-mini`로 변경되었습니다. 이는 모델을 명시적으로 설정하지 않은 에이전트와 실행에 영향을 줍니다. 새로운 기본 모델이 GPT-5 모델이므로 암시적 기본 모델 설정에도 `reasoning.effort="none"` 및 `verbosity="low"`와 같은 GPT-5 기본값이 포함됩니다.

이전의 기본 모델 동작을 유지해야 한다면 에이전트나 실행 구성에서 모델을 명시적으로 설정하거나 `OPENAI_DEFAULT_MODEL` 환경 변수를 설정하세요.

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

주요 내용:

-   이제 `Runner.run`, `Runner.run_sync` 및 `Runner.run_streamed`에서 `max_turns=None`을 지정하여 턴 제한을 비활성화할 수 있습니다.
-   이제 로컬, Docker 및 공급자 기반 샌드박스 구현 전반에서 샌드박스 워크스페이스 하이드레이션이 절대 경로 심볼릭 링크 대상을 포함하여 아카이브 루트 외부를 가리키는 심볼릭 링크가 있는 tar 아카이브를 거부합니다.

### 0.15.0

이 버전에서는 모델 거부 응답이 빈 텍스트 출력으로 처리되거나 structured outputs의 경우 실행 루프가 `MaxTurnsExceeded`에 도달할 때까지 재시도하게 만드는 대신, 이제 `ModelRefusalError`로 명시적으로 노출됩니다.

이는 이전에 거부 응답만 포함된 모델 응답이 `final_output == ""`인 상태로 완료될 것으로 예상했던 코드에 영향을 줍니다. 예외를 발생시키지 않고 거부 응답을 처리하려면 `model_refusal` 실행 오류 핸들러를 제공하세요.

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

구조화된 출력을 사용하는 에이전트의 경우 핸들러가 에이전트의 출력 스키마와 일치하는 값을 반환할 수 있으며, SDK는 다른 실행 오류 핸들러의 최종 출력과 동일하게 이를 검증합니다.

### 0.14.0

이번 마이너 릴리스에는 호환성을 깨는 변경 사항이 **포함되지 않지만**, 새로운 주요 베타 기능 영역인 샌드박스 에이전트(Sandbox Agents)와 이를 로컬, 컨테이너화 및 호스팅 환경 전반에서 사용하는 데 필요한 런타임, 백엔드 및 문서 지원이 추가되었습니다.

주요 내용:

-   `SandboxAgent`, `Manifest` 및 `SandboxRunConfig`를 중심으로 하는 새로운 베타 샌드박스 런타임 인터페이스를 추가하여 에이전트가 파일, 디렉터리, Git 저장소, 마운트, 스냅샷 및 재개 기능을 갖춘 영구 격리 워크스페이스 내부에서 작업할 수 있도록 했습니다.
-   `UnixLocalSandboxClient` 및 `DockerSandboxClient`를 통해 로컬 및 컨테이너화된 개발을 위한 샌드박스 실행 백엔드를 추가하고, 선택적 추가 종속성을 통해 Blaxel, Cloudflare, Daytona, E2B, Modal, Runloop 및 Vercel의 호스팅 공급자 통합을 추가했습니다.
-   이후 실행에서 이전 실행의 교훈을 재사용할 수 있도록 샌드박스 메모리 지원을 추가했습니다. 여기에는 점진적 공개, 멀티턴 그룹화, 구성 가능한 격리 경계 및 S3 기반 워크플로를 포함한 영속 메모리 예제가 포함됩니다.
-   로컬 및 합성 워크스페이스 항목, S3/R2/GCS/Azure Blob Storage/S3 Files용 원격 스토리지 마운트, 이식 가능한 스냅샷, `RunState`, `SandboxSessionState` 또는 저장된 스냅샷을 통한 재개 흐름을 포함하여 워크스페이스 및 재개 모델을 확장했습니다.
-   `examples/sandbox/` 아래에 스킬, 핸드오프, 메모리를 사용하는 코딩 작업, 공급자별 설정, 코드 리뷰, 데이터룸 QA 및 웹사이트 복제와 같은 엔드투엔드 워크플로를 다루는 다양한 샌드박스 코드 예제와 튜토리얼을 추가했습니다.
-   샌드박스를 인식하는 세션 준비, 기능 바인딩, 상태 직렬화, 통합 트레이싱, 프롬프트 캐시 키 기본값 및 더욱 안전한 민감한 MCP 출력 마스킹을 통해 핵심 런타임과 트레이싱 스택을 확장했습니다.

### 0.13.0

이번 마이너 릴리스에는 호환성을 깨는 변경 사항이 **포함되지 않지만**, 주목할 만한 Realtime 기본값 업데이트와 새로운 MCP 기능 및 런타임 안정성 수정 사항이 포함되었습니다.

주요 내용:

-   이제 기본 WebSocket Realtime 모델은 `gpt-realtime-1.5`이므로, 새로운 Realtime 에이전트 설정에서는 별도의 구성 없이 더 최신 모델을 사용합니다.
-   이제 `MCPServer`는 `list_resources()`, `list_resource_templates()` 및 `read_resource()`를 제공하며, `MCPServerStreamableHttp`는 `session_id`를 제공하므로 스트리밍 가능 HTTP 세션을 재연결하거나 상태 비저장 워커 간에 재개할 수 있습니다.
-   이제 Chat Completions 통합에서 `should_replay_reasoning_content`를 통해 추론 콘텐츠 재생을 선택적으로 활성화할 수 있어 LiteLLM/DeepSeek 같은 어댑터의 공급자별 추론/도구 호출 연속성이 향상됩니다.
-   `SQLAlchemySession`에서 동시에 수행되는 최초 쓰기, 추론 제거 후 고립된 어시스턴트 메시지 ID가 포함된 압축 요청, `remove_all_tools()`가 MCP/추론 항목을 남기는 문제, 함수 도구 배치 실행기의 경합 조건을 포함하여 여러 런타임 및 세션 경계 사례를 수정했습니다.

### 0.12.0

이번 마이너 릴리스에는 호환성을 깨는 변경 사항이 **포함되지 않습니다**. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)를 참조하세요.

### 0.11.0

이번 마이너 릴리스에는 호환성을 깨는 변경 사항이 **포함되지 않습니다**. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)를 참조하세요.

### 0.10.0

이번 마이너 릴리스에는 호환성을 깨는 변경 사항이 **포함되지 않지만**, OpenAI Responses 사용자를 위한 주요 신규 기능 영역인 Responses API의 WebSocket 전송 지원이 포함되었습니다.

주요 내용:

-   OpenAI Responses 모델에 대한 WebSocket 전송 지원을 추가했습니다. 이는 선택적으로 활성화할 수 있으며, HTTP는 계속 기본 전송 방식으로 사용됩니다.
-   멀티턴 실행 전반에서 공유 WebSocket 지원 공급자와 `RunConfig`를 재사용할 수 있도록 `responses_websocket_session()` 헬퍼와 `ResponsesWebSocketSession`을 추가했습니다.
-   스트리밍, 도구, 승인 및 후속 턴을 다루는 새로운 WebSocket 스트리밍 예제(`examples/basic/stream_ws.py`)를 추가했습니다.

### 0.9.0

이 버전에서는 Python 3.9가 더 이상 지원되지 않습니다. 해당 메이저 버전이 3개월 전에 지원 종료(EOL)에 도달했기 때문입니다. 더 최신 런타임 버전으로 업그레이드하세요.

또한 `Agent#as_tool()` 메서드가 반환하는 값의 타입 힌트가 `Tool`에서 `FunctionTool`로 좁아졌습니다. 이 변경은 일반적으로 호환성을 깨는 문제를 일으키지 않지만, 코드에서 더 넓은 유니온 타입에 의존하는 경우 일부 조정이 필요할 수 있습니다.

### 0.8.0

이 버전에서는 다음 두 가지 런타임 동작 변경으로 인해 마이그레이션 작업이 필요할 수 있습니다.

- **동기식** Python 호출 가능 객체를 래핑하는 함수 도구는 이제 이벤트 루프 스레드에서 실행되는 대신 `asyncio.to_thread(...)`를 통해 워커 스레드에서 실행됩니다. 도구 로직이 스레드 로컬 상태나 특정 스레드에 종속된 리소스에 의존한다면 비동기 도구 구현으로 마이그레이션하거나 도구 코드에서 스레드 종속성을 명시적으로 지정하세요.
- 이제 로컬 MCP 도구 실패 처리를 구성할 수 있으며, 기본 동작은 전체 실행을 실패시키는 대신 모델에 표시되는 오류 출력을 반환할 수 있습니다. 빠른 실패 동작에 의존한다면 `mcp_config={"failure_error_function": None}`을 설정하세요. 서버 수준의 `failure_error_function` 값은 에이전트 수준 설정을 재정의하므로, 명시적 핸들러가 있는 각 로컬 MCP 서버에서 `failure_error_function=None`을 설정하세요.

### 0.7.0

이 버전에서는 기존 애플리케이션에 영향을 줄 수 있는 몇 가지 동작이 변경되었습니다.

- 중첩 핸드오프 기록은 이제 **선택적 활성화 방식**이며 기본적으로 비활성화됩니다. v0.6.x의 기본 중첩 동작에 의존했다면 `RunConfig(nest_handoff_history=True)`를 명시적으로 설정하세요.
- `gpt-5.1` / `gpt-5.2`의 기본 `reasoning.effort`가 SDK 기본값으로 구성되었던 이전 기본값 `"low"`에서 `"none"`으로 변경되었습니다. 프롬프트나 품질/비용 프로필이 `"low"`에 의존했다면 `model_settings`에서 이를 명시적으로 설정하세요.

### 0.6.0

이 버전에서는 기본 핸드오프 기록이 사용자/어시스턴트 턴 원문을 노출하는 대신 단일 어시스턴트 메시지로 묶이므로 다운스트림 에이전트에 간결하고 예측 가능한 요약을 제공합니다
- 기존 단일 메시지 핸드오프 기록은 이제 기본적으로 `<CONVERSATION HISTORY>` 블록 앞에서 "For context, here is the conversation so far between the user and the previous agent:"로 시작하므로 다운스트림 에이전트가 명확한 레이블이 지정된 요약을 받습니다

### 0.5.0

이 버전에는 눈에 보이는 호환성을 깨는 변경 사항이 없지만, 새로운 기능과 몇 가지 중요한 내부 업데이트가 포함되었습니다.

- `RealtimeRunner`에서 [SIP 프로토콜 연결](https://platform.openai.com/docs/guides/realtime-sip)을 처리할 수 있도록 지원을 추가했습니다.
- Python 3.14 호환성을 위해 `Runner#run_sync`의 내부 로직을 대폭 수정했습니다.

### 0.4.0

이 버전에서는 [openai](https://pypi.org/project/openai/) 패키지 v1.x 버전이 더 이상 지원되지 않습니다. 이 SDK와 함께 openai v2.x를 사용하세요.

### 0.3.0

이 버전에서는 Realtime API 지원이 gpt-realtime 모델과 해당 API 인터페이스(GA 버전)로 전환됩니다.

### 0.2.0

이 버전에서는 이전에 `Agent`를 인수로 받던 몇몇 부분이 이제 `AgentBase`를 인수로 받습니다. 예를 들어 MCP 서버의 `list_tools()` 호출이 이에 해당합니다. 이는 순수한 타입 변경이며, 계속해서 `Agent` 객체를 받게 됩니다. 업데이트하려면 `Agent`를 `AgentBase`로 바꿔 타입 오류를 수정하면 됩니다.

### 0.1.0

이 버전에서는 [`MCPServer.list_tools()`][agents.mcp.server.MCPServer]에 `run_context`와 `agent`라는 새로운 매개변수 두 개가 추가되었습니다. `MCPServer`를 상속하는 모든 클래스에 이러한 매개변수를 추가해야 합니다.