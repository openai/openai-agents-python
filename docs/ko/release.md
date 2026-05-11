---
search:
  exclude: true
---
# 릴리스 프로세스/변경 로그

이 프로젝트는 `0.Y.Z` 형식을 사용하는, 약간 수정된 의미적 버전 관리를 따릅니다. 앞의 `0`은 SDK가 아직 빠르게 발전하고 있음을 나타냅니다. 각 구성 요소는 다음과 같이 증가합니다.

## 마이너(`Y`) 버전

베타로 표시되지 않은 공개 인터페이스에 대한 **호환성이 깨지는 변경**이 있을 때 마이너 버전 `Y`를 올립니다. 예를 들어 `0.0.x`에서 `0.1.x`로 올라갈 때 호환성이 깨지는 변경이 포함될 수 있습니다.

호환성이 깨지는 변경을 원하지 않는 경우, 프로젝트에서 `0.0.x` 버전으로 고정하는 것을 권장합니다.

## 패치(`Z`) 버전

호환성을 깨지 않는 변경에는 `Z`를 증가시킵니다.

- 버그 수정
- 새로운 기능
- 비공개 인터페이스 변경
- 베타 기능 업데이트

## 호환성이 깨지는 변경 로그

### 0.17.0

이 버전에서는 소스 경로가 `Manifest.extra_path_grants`에 의해 포함되지 않는 한, 샌드박스 로컬 소스 구체화가 `LocalFile.src`와 `LocalDir.src`를 구체화 `base_dir` 안에 유지합니다. `base_dir`은 매니페스트가 적용될 때 SDK 프로세스의 현재 작업 디렉터리입니다. 상대 로컬 소스는 해당 디렉터리에서 해석되며, 절대 로컬 소스는 이미 그 안에 있거나 명시적 허가 아래에 있어야 합니다. 이는 로컬 아티팩트 경계 문제를 해결하지만, 해당 기본 디렉터리 외부의 신뢰할 수 있는 호스트 파일이나 디렉터리를 샌드박스 작업 공간으로 의도적으로 복사하는 애플리케이션에 영향을 줄 수 있습니다.

마이그레이션하려면 `SandboxPathGrant`를 사용해 매니페스트 수준에서 신뢰할 수 있는 호스트 루트에 권한을 부여하고, 샌드박스가 해당 파일을 읽기만 하면 되는 경우에는 읽기 전용으로 설정하는 것이 좋습니다.

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

`extra_path_grants`는 신뢰할 수 있는 애플리케이션 구성으로 취급하세요. 애플리케이션이 해당 호스트 경로를 이미 승인하지 않은 한, 모델 출력이나 기타 신뢰할 수 없는 매니페스트 입력에서 권한 부여 항목을 채우지 마세요.

### 0.16.0

이 버전에서는 SDK 기본 모델이 `gpt-4.1` 대신 `gpt-5.4-mini`가 되었습니다. 이는 모델을 명시적으로 설정하지 않은 에이전트와 실행에 영향을 줍니다. 새 기본값이 GPT-5 모델이므로, 암시적 기본 모델 설정에는 이제 `reasoning.effort="none"` 및 `verbosity="low"` 같은 GPT-5 기본값이 포함됩니다.

이전 기본 모델 동작을 유지해야 한다면, 에이전트 또는 실행 구성에 모델을 명시적으로 설정하거나 `OPENAI_DEFAULT_MODEL` 환경 변수를 설정하세요.

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

주요 내용:

- `Runner.run`, `Runner.run_sync`, `Runner.run_streamed`는 이제 턴 제한을 비활성화하기 위해 `max_turns=None`을 허용합니다.
- 샌드박스 작업 공간 하이드레이션은 이제 로컬, Docker, 제공자 지원 샌드박스 구현 전반에서 절대 심볼릭 링크 대상을 포함하여 아카이브 루트 외부를 가리키는 심볼릭 링크가 있는 tar 아카이브를 거부합니다.

### 0.15.0

이 버전에서는 모델 거부가 빈 텍스트 출력으로 처리되거나, structured outputs의 경우 실행 루프가 `MaxTurnsExceeded`가 될 때까지 재시도하게 하는 대신, `ModelRefusalError`로 명시적으로 드러납니다.

이는 이전에 거부만 포함된 모델 응답이 `final_output == ""`로 완료될 것으로 기대하던 코드에 영향을 줍니다. 예외를 발생시키지 않고 거부를 처리하려면 `model_refusal` 실행 오류 핸들러를 제공하세요.

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

structured-output 에이전트의 경우, 핸들러는 에이전트의 출력 스키마와 일치하는 값을 반환할 수 있으며 SDK는 이를 다른 실행 오류 핸들러의 최종 출력과 마찬가지로 검증합니다.

### 0.14.0

이 마이너 릴리스는 호환성이 깨지는 변경을 도입하지는 **않지만**, 주요한 새 베타 기능 영역인 Sandbox Agents와 이를 로컬, 컨테이너화된 환경, 호스팅 환경 전반에서 사용하는 데 필요한 런타임, 백엔드, 문서 지원을 추가합니다.

주요 내용:

- `SandboxAgent`, `Manifest`, `SandboxRunConfig`를 중심으로 한 새로운 베타 샌드박스 런타임 표면을 추가하여, 에이전트가 파일, 디렉터리, Git 리포지터리, 마운트, 스냅샷, 재개 지원이 있는 영구 격리 작업 공간 안에서 작업할 수 있게 했습니다.
- `UnixLocalSandboxClient` 및 `DockerSandboxClient`를 통해 로컬 및 컨테이너화된 개발을 위한 샌드박스 실행 백엔드를 추가했으며, 선택적 extras를 통해 Blaxel, Cloudflare, Daytona, E2B, Modal, Runloop, Vercel에 대한 호스팅 제공자 통합을 추가했습니다.
- 향후 실행이 이전 실행의 교훈을 재사용할 수 있도록 샌드박스 메모리 지원을 추가했으며, 점진적 공개, 멀티 턴 그룹화, 구성 가능한 격리 경계, S3 기반 워크플로를 포함한 영속 메모리 예제를 제공합니다.
- 로컬 및 합성 작업 공간 항목, S3/R2/GCS/Azure Blob Storage/S3 Files용 원격 스토리지 마운트, 이식 가능한 스냅샷, `RunState`, `SandboxSessionState` 또는 저장된 스냅샷을 통한 재개 흐름을 포함하여 더 넓은 작업 공간 및 재개 모델을 추가했습니다.
- `examples/sandbox/` 아래에 기술을 사용한 코딩 작업, 핸드오프, 메모리, 제공자별 설정, 코드 리뷰, 데이터룸 QA, 웹사이트 클로닝 같은 엔드투엔드 워크플로를 다루는 상당한 규모의 샌드박스 예제와 튜토리얼을 추가했습니다.
- 샌드박스 인식 세션 준비, 기능 바인딩, 상태 직렬화, 통합 트레이싱, 프롬프트 캐시 키 기본값, 더 안전한 민감한 MCP 출력 비식별화를 통해 코어 런타임과 트레이싱 스택을 확장했습니다.

### 0.13.0

이 마이너 릴리스는 호환성이 깨지는 변경을 도입하지는 **않지만**, 주목할 만한 Realtime 기본값 업데이트와 새로운 MCP 기능, 런타임 안정성 수정 사항을 포함합니다.

주요 내용:

- 기본 websocket Realtime 모델이 이제 `gpt-realtime-1.5`이므로, 새로운 Realtime 에이전트 설정은 추가 구성 없이 더 새로운 모델을 사용합니다.
- `MCPServer`는 이제 `list_resources()`, `list_resource_templates()`, `read_resource()`를 노출하며, `MCPServerStreamableHttp`는 이제 `session_id`를 노출하여 streamable HTTP 세션을 재연결 또는 상태 비저장 워커 간에 재개할 수 있습니다.
- Chat Completions 통합은 이제 `should_replay_reasoning_content`를 통해 reasoning-content replay를 선택적으로 사용할 수 있어, LiteLLM/DeepSeek 같은 어댑터에서 제공자별 추론/도구 호출 연속성이 향상됩니다.
- `SQLAlchemySession`의 동시 첫 쓰기, reasoning 제거 후 고아 assistant 메시지 ID가 있는 compaction 요청, `remove_all_tools()`가 MCP/reasoning 항목을 남기는 문제, 함수 도구 배치 실행기의 경합 등 여러 런타임 및 세션 엣지 케이스를 수정했습니다.

### 0.12.0

이 마이너 릴리스는 호환성이 깨지는 변경을 도입하지 **않습니다**. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0)를 확인하세요.

### 0.11.0

이 마이너 릴리스는 호환성이 깨지는 변경을 도입하지 **않습니다**. 주요 기능 추가 사항은 [릴리스 노트](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0)를 확인하세요.

### 0.10.0

이 마이너 릴리스는 호환성이 깨지는 변경을 도입하지는 **않지만**, OpenAI Responses 사용자를 위한 중요한 새 기능 영역인 Responses API용 websocket 전송 지원을 포함합니다.

주요 내용:

- OpenAI Responses 모델을 위한 websocket 전송 지원을 추가했습니다(옵트인; HTTP는 기본 전송으로 유지됩니다).
- 멀티 턴 실행 전반에서 공유 websocket 지원 제공자와 `RunConfig`를 재사용하기 위한 `responses_websocket_session()` 헬퍼 / `ResponsesWebSocketSession`을 추가했습니다.
- 스트리밍, 도구, 승인, 후속 턴을 다루는 새로운 websocket 스트리밍 예제(`examples/basic/stream_ws.py`)를 추가했습니다.

### 0.9.0

이 버전에서는 Python 3.9가 더 이상 지원되지 않습니다. 이 주 버전은 3개월 전에 EOL에 도달했습니다. 더 최신 런타임 버전으로 업그레이드하세요.

또한 `Agent#as_tool()` 메서드에서 반환되는 값의 타입 힌트가 `Tool`에서 `FunctionTool`로 좁혀졌습니다. 이 변경은 일반적으로 호환성 문제를 일으키지 않지만, 코드가 더 넓은 유니온 타입에 의존하는 경우 일부 조정이 필요할 수 있습니다.

### 0.8.0

이 버전에서는 두 가지 런타임 동작 변경으로 마이그레이션 작업이 필요할 수 있습니다.

- **동기** Python 호출 가능 객체를 래핑하는 함수 도구는 이제 이벤트 루프 스레드에서 실행되는 대신 `asyncio.to_thread(...)`를 통해 워커 스레드에서 실행됩니다. 도구 로직이 스레드 로컬 상태 또는 스레드 종속 리소스에 의존한다면, 비동기 도구 구현으로 마이그레이션하거나 도구 코드에서 스레드 종속성을 명시적으로 처리하세요.
- 로컬 MCP 도구 실패 처리가 이제 구성 가능하며, 기본 동작은 전체 실행을 실패시키는 대신 모델에 보이는 오류 출력을 반환할 수 있습니다. 빠른 실패(fail-fast) 의미 체계에 의존한다면 `mcp_config={"failure_error_function": None}`을 설정하세요. 서버 수준 `failure_error_function` 값은 에이전트 수준 설정을 재정의하므로, 명시적 핸들러가 있는 각 로컬 MCP 서버에 `failure_error_function=None`을 설정하세요.

### 0.7.0

이 버전에는 기존 애플리케이션에 영향을 줄 수 있는 몇 가지 동작 변경이 있었습니다.

- 중첩 핸드오프 기록은 이제 **옵트인**입니다(기본적으로 비활성화됨). v0.6.x의 기본 중첩 동작에 의존했다면 `RunConfig(nest_handoff_history=True)`를 명시적으로 설정하세요.
- `gpt-5.1` / `gpt-5.2`의 기본 `reasoning.effort`가 `"none"`으로 변경되었습니다(SDK 기본값으로 구성된 이전 기본값 `"low"`에서 변경). 프롬프트 또는 품질/비용 프로필이 `"low"`에 의존했다면 `model_settings`에서 명시적으로 설정하세요.

### 0.6.0

이 버전에서는 기본 핸드오프 기록이 이제 원문 사용자/assistant 턴을 노출하는 대신 하나의 assistant 메시지로 패키징되어, 다운스트림 에이전트에 간결하고 예측 가능한 요약을 제공합니다
- 기존 단일 메시지 핸드오프 transcript는 이제 기본적으로 `<CONVERSATION HISTORY>` 블록 앞에 "For context, here is the conversation so far between the user and the previous agent:"로 시작하므로, 다운스트림 에이전트가 명확히 라벨링된 요약을 받습니다

### 0.5.0

이 버전은 눈에 보이는 호환성이 깨지는 변경을 도입하지 않지만, 새로운 기능과 내부의 몇 가지 중요한 업데이트를 포함합니다.

- `RealtimeRunner`가 [SIP 프로토콜 연결](https://platform.openai.com/docs/guides/realtime-sip)을 처리할 수 있도록 지원을 추가했습니다
- Python 3.14 호환성을 위해 `Runner#run_sync`의 내부 로직을 크게 개정했습니다

### 0.4.0

이 버전에서는 [openai](https://pypi.org/project/openai/) 패키지 v1.x 버전이 더 이상 지원되지 않습니다. 이 SDK와 함께 openai v2.x를 사용하세요.

### 0.3.0

이 버전에서는 Realtime API 지원이 gpt-realtime 모델과 그 API 인터페이스(GA 버전)로 마이그레이션됩니다.

### 0.2.0

이 버전에서는 이전에 `Agent`를 인수로 받던 몇몇 위치가 이제 대신 `AgentBase`를 인수로 받습니다. 예를 들어 MCP 서버의 `list_tools()` 호출이 있습니다. 이는 순수한 타입 변경이며, 여전히 `Agent` 객체를 받게 됩니다. 업데이트하려면 `Agent`를 `AgentBase`로 바꾸어 타입 오류만 수정하면 됩니다.

### 0.1.0

이 버전에서는 [`MCPServer.list_tools()`][agents.mcp.server.MCPServer]에 `run_context`와 `agent`라는 두 개의 새 매개변수가 있습니다. `MCPServer`를 서브클래싱하는 모든 클래스에 이 매개변수를 추가해야 합니다.