---
search:
  exclude: true
---
# 빠른 시작

!!! warning "베타 기능"

    샌드박스 에이전트는 베타입니다. 정식 출시 전에 API 세부 사항, 기본값, 지원 기능이 변경될 수 있으며, 시간이 지나면서 더 고급 기능이 추가될 예정입니다

현대적인 에이전트는 파일시스템의 실제 파일에서 작업할 수 있을 때 가장 잘 동작합니다. Agents SDK의 **Sandbox Agents**는 모델에 지속적인 워크스페이스를 제공하여, 대규모 문서 집합 검색, 파일 편집, 명령 실행, 아티팩트 생성, 저장된 샌드박스 상태에서 작업 재개를 가능하게 합니다

SDK는 파일 스테이징, 파일시스템 도구, 셸 접근, 샌드박스 수명 주기, 스냅샷, 공급자별 연결 코드를 직접 조합하지 않아도 이 실행 하네스를 제공합니다. 일반적인 `Agent` 및 `Runner` 흐름을 유지한 채, 워크스페이스용 `Manifest`, 샌드박스 네이티브 도구용 capabilities, 작업 실행 위치를 위한 `SandboxRunConfig`를 추가하면 됩니다

## 사전 요구 사항

- Python 3.10 이상
- OpenAI Agents SDK에 대한 기본적인 이해
- 샌드박스 클라이언트. 로컬 개발의 경우 `UnixLocalSandboxClient`로 시작하세요

## 설치

아직 SDK를 설치하지 않았다면:

```bash
pip install openai-agents
```

Docker 기반 샌드박스의 경우:

```bash
pip install "openai-agents[docker]"
```

## 로컬 샌드박스 에이전트 생성

이 예제는 `repo/` 아래에 로컬 리포지토리를 스테이징하고, 로컬 스킬을 지연 로드하며, 실행 시 runner가 Unix 로컬 샌드박스 세션을 생성하도록 합니다

```python
import asyncio
from pathlib import Path

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Capabilities, LocalDirLazySkillSource, Skills
from agents.sandbox.entries import LocalDir
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

EXAMPLE_DIR = Path(__file__).resolve().parent
HOST_REPO_DIR = EXAMPLE_DIR / "repo"
HOST_SKILLS_DIR = EXAMPLE_DIR / "skills"


def build_agent(model: str) -> SandboxAgent[None]:
    return SandboxAgent(
        name="Sandbox engineer",
        model=model,
        instructions=(
            "Read `repo/task.md` before editing files. Stay grounded in the repository, preserve "
            "existing behavior, and mention the exact verification command you ran. "
            "If you edit files with apply_patch, paths are relative to the sandbox workspace root."
        ),
        default_manifest=Manifest(
            entries={
                "repo": LocalDir(src=HOST_REPO_DIR),
            }
        ),
        capabilities=Capabilities.default() + [
            Skills(
                lazy_from=LocalDirLazySkillSource(
                    source=LocalDir(src=HOST_SKILLS_DIR),
                )
            ),
        ],
    )


async def main() -> None:
    result = await Runner.run(
        build_agent("gpt-5.4"),
        "Open `repo/task.md`, fix the issue, run the targeted test, and summarize the change.",
        run_config=RunConfig(
            sandbox=SandboxRunConfig(client=UnixLocalSandboxClient()),
            workflow_name="Sandbox coding example",
        ),
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

[examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py)를 참고하세요. 이 예제는 작은 셸 기반 리포지토리를 사용하므로 Unix 로컬 실행 전반에서 결정적으로 검증할 수 있습니다

## 주요 선택 사항

기본 실행이 동작한 뒤, 다음으로 가장 많이 선택하는 항목은 다음과 같습니다:

- `default_manifest`: 새 샌드박스 세션을 위한 파일, 리포지토리, 디렉터리, 마운트
- `instructions`: 프롬프트 전반에 적용되어야 하는 짧은 워크플로 규칙
- `base_instructions`: SDK 샌드박스 프롬프트를 대체하기 위한 고급 이스케이프 해치
- `capabilities`: 파일시스템 편집/이미지 검사, 셸, 스킬, 메모리, 컴팩션 등의 샌드박스 네이티브 도구
- `run_as`: 모델 대상 도구에서 사용할 샌드박스 사용자 신원
- `SandboxRunConfig.client`: 샌드박스 백엔드
- `SandboxRunConfig.session`, `session_state`, 또는 `snapshot`: 이후 실행이 이전 작업에 재연결되는 방식

## 다음 단계

- [개념](sandbox/guide.md): 매니페스트, capabilities, 권한, 스냅샷, 실행 구성, 구성 패턴을 이해합니다
- [샌드박스 클라이언트](sandbox/clients.md): Unix 로컬, Docker, 호스티드 공급자, 마운트 전략 중에서 선택합니다
- [에이전트 메모리](sandbox/memory.md): 이전 샌드박스 실행의 학습 내용을 보존하고 재사용합니다

셸 접근이 가끔 사용하는 도구 하나에 불과하다면 [도구 가이드](tools.md)의 호스티드 셸로 시작하세요. 워크스페이스 격리, 샌드박스 클라이언트 선택, 또는 샌드박스 세션 재개 동작이 설계의 일부라면 샌드박스 에이전트를 사용하세요