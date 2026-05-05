---
search:
  exclude: true
---
# 에이전트 메모리

메모리를 사용하면 이후의 sandbox-agent 실행이 이전 실행에서 학습할 수 있습니다. 이는 메시지 기록을 저장하는 SDK의 대화형 [`Session`](../sessions/index.md) 메모리와는 별개입니다. 메모리는 이전 실행에서 얻은 교훈을 sandbox 워크스페이스의 파일로 정리합니다.

!!! warning "베타 기능"

    Sandbox 에이전트는 베타입니다. 일반 제공 이전에 API의 세부 사항, 기본값, 지원 기능이 변경될 수 있으며, 시간이 지나면서 더 고급 기능이 추가될 수 있습니다.

메모리는 이후 실행에서 세 가지 종류의 비용을 줄일 수 있습니다.

1. 에이전트 비용: 에이전트가 워크플로를 완료하는 데 오랜 시간이 걸렸다면, 다음 실행에서는 탐색이 덜 필요해야 합니다. 이렇게 하면 토큰 사용량과 완료 시간을 줄일 수 있습니다.
2. 사용자 비용: 사용자가 에이전트를 수정했거나 선호 사항을 표현했다면, 이후 실행은 그 피드백을 기억할 수 있습니다. 이렇게 하면 사람의 개입을 줄일 수 있습니다.
3. 컨텍스트 비용: 에이전트가 이전에 작업을 완료했고 사용자가 그 작업을 이어서 진행하려는 경우, 사용자는 이전 스레드를 찾거나 모든 컨텍스트를 다시 입력할 필요가 없어야 합니다. 이렇게 하면 작업 설명이 더 짧아집니다.

버그를 수정하고, 메모리를 생성하고, 스냅샷을 재개하고, 후속 검증 실행에서 해당 메모리를 사용하는 완전한 2회 실행 예제는 [examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py)를 참조하세요. 별도의 메모리 레이아웃을 사용하는 멀티턴, 멀티 에이전트 예제는 [examples/sandbox/memory_multi_agent_multiturn.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory_multi_agent_multiturn.py)를 참조하세요.

## 메모리 활성화

sandbox 에이전트의 capability로 `Memory()`를 추가합니다.

```python
from pathlib import Path
import tempfile

from agents.sandbox import LocalSnapshotSpec, SandboxAgent
from agents.sandbox.capabilities import Filesystem, Memory, Shell

agent = SandboxAgent(
    name="Memory-enabled reviewer",
    instructions="Inspect the workspace and preserve useful lessons for follow-up runs.",
    capabilities=[Memory(), Filesystem(), Shell()],
)

with tempfile.TemporaryDirectory(prefix="sandbox-memory-example-") as snapshot_dir:
    sandbox = await client.create(
        manifest=manifest,
        snapshot=LocalSnapshotSpec(base_path=Path(snapshot_dir)),
    )
```

읽기가 활성화되면 `Memory()`에는 `Shell()`이 필요하며, 이를 통해 주입된 요약만으로 충분하지 않을 때 에이전트가 메모리 파일을 읽고 검색할 수 있습니다. 라이브 메모리 업데이트가 활성화된 경우(기본값)에는 `Filesystem()`도 필요하며, 이를 통해 에이전트가 오래된 메모리를 발견했거나 사용자가 메모리 업데이트를 요청했을 때 `memories/MEMORY.md`를 업데이트할 수 있습니다.

기본적으로 메모리 아티팩트는 sandbox 워크스페이스의 `memories/` 아래에 저장됩니다. 이후 실행에서 이를 재사용하려면 동일한 라이브 sandbox 세션을 유지하거나, 영속화된 세션 상태 또는 스냅샷에서 재개하여 구성된 전체 memories 디렉터리를 보존하고 재사용해야 합니다. 새 빈 sandbox는 빈 메모리로 시작합니다.

`Memory()`는 메모리 읽기와 메모리 생성을 모두 활성화합니다. 메모리를 읽되 새 메모리는 생성하지 않아야 하는 에이전트에는 `Memory(generate=None)`를 사용하세요. 예를 들어, 내부 에이전트, 서브에이전트, 검사기, 또는 실행이 큰 신호를 추가하지 않는 일회성 도구 에이전트가 이에 해당합니다. 실행이 나중을 위해 메모리를 생성해야 하지만, 사용자가 기존 메모리의 영향을 받기를 원하지 않는 경우에는 `Memory(read=None)`를 사용하세요.

## 메모리 읽기

메모리 읽기는 점진적 공개(progressive disclosure)를 사용합니다. 실행 시작 시 SDK는 일반적으로 유용한 팁, 사용자 선호 사항, 사용 가능한 메모리를 담은 작은 요약인 (`memory_summary.md`)을 에이전트의 개발자 프롬프트에 주입합니다. 이를 통해 에이전트는 이전 작업이 관련 있을 수 있는지 판단할 만큼 충분한 컨텍스트를 얻습니다.

이전 작업이 관련 있어 보이면, 에이전트는 현재 작업의 키워드로 구성된 메모리 인덱스(`memories_dir` 아래의 `MEMORY.md`)를 검색합니다. 더 자세한 정보가 필요한 경우에만 구성된 `rollout_summaries/` 디렉터리 아래의 해당 이전 rollout 요약을 엽니다.

메모리는 오래될 수 있습니다. 에이전트는 메모리를 오직 참고용으로만 취급하고 현재 환경을 신뢰하도록 지시받습니다. 기본적으로 메모리 읽기에는 `live_update`가 활성화되어 있으므로, 에이전트가 오래된 메모리를 발견하면 같은 실행에서 구성된 `MEMORY.md`를 업데이트할 수 있습니다. 예를 들어 실행이 지연 시간에 민감한 경우처럼, 에이전트가 메모리를 읽되 실행 중 수정해서는 안 되는 경우에는 라이브 업데이트를 비활성화하세요.

## 메모리 생성

실행이 끝나면 sandbox 런타임은 해당 실행 세그먼트를 대화 파일에 추가합니다. 누적된 대화 파일은 sandbox 세션이 닫힐 때 처리됩니다.

메모리 생성에는 두 단계가 있습니다.

1. 1단계: 대화 추출. 메모리 생성 모델이 하나의 누적된 대화 파일을 처리하고 대화 요약을 생성합니다. 시스템, 개발자, 추론 콘텐츠는 제외됩니다. 대화가 너무 길면 컨텍스트 윈도에 맞도록 잘리며, 시작과 끝은 보존됩니다. 또한 2단계에서 통합할 수 있도록 대화의 간결한 메모인 원문 메모리 추출도 생성합니다.
2. 2단계: 레이아웃 통합. 통합 에이전트가 하나의 메모리 레이아웃에 대한 원문 메모리를 읽고, 더 많은 근거가 필요할 때 대화 요약을 열어 패턴을 `MEMORY.md`와 `memory_summary.md`로 추출합니다.

기본 워크스페이스 레이아웃은 다음과 같습니다.

```text
workspace/
├── sessions/
│   └── <rollout-id>.jsonl
└── memories/
    ├── memory_summary.md
    ├── MEMORY.md
    ├── raw_memories.md (intermediate)
    ├── phase_two_selection.json (intermediate)
    ├── raw_memories/ (intermediate)
    │   └── <rollout-id>.md
    ├── rollout_summaries/
    │   └── <rollout-id>_<slug>.md
    └── skills/
```

`MemoryGenerateConfig`로 메모리 생성을 구성할 수 있습니다.

```python
from agents.sandbox import MemoryGenerateConfig
from agents.sandbox.capabilities import Memory

memory = Memory(
    generate=MemoryGenerateConfig(
        max_raw_memories_for_consolidation=128,
        extra_prompt="Pay extra attention to what made the customer more satisfied or annoyed",
    ),
)
```

`extra_prompt`를 사용해 GTM 에이전트의 고객 및 회사 세부 정보처럼, 사용 사례에서 어떤 신호가 가장 중요한지 메모리 생성기에 알려주세요.

최근 원문 메모리가 `max_raw_memories_for_consolidation`(기본값 256)을 초과하면, 2단계는 가장 최신 대화의 메모리만 유지하고 오래된 것은 제거합니다. 최신성은 대화가 마지막으로 업데이트된 시간을 기준으로 합니다. 이 망각 메커니즘은 메모리가 가장 새로운 환경을 반영하도록 돕습니다.

## 멀티턴 대화

멀티턴 sandbox 채팅의 경우, 동일한 라이브 sandbox 세션과 함께 일반 SDK `Session`을 사용하세요.

```python
from agents import Runner, SQLiteSession
from agents.run import RunConfig
from agents.sandbox import SandboxRunConfig

conversation_session = SQLiteSession("gtm-q2-pipeline-review")
sandbox = await client.create(manifest=agent.default_manifest)

async with sandbox:
    run_config = RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
        workflow_name="GTM memory example",
    )
    await Runner.run(
        agent,
        "Analyze data/leads.csv and identify one promising GTM segment.",
        session=conversation_session,
        run_config=run_config,
    )
    await Runner.run(
        agent,
        "Using that analysis, write a short outreach hypothesis.",
        session=conversation_session,
        run_config=run_config,
    )
```

두 실행은 동일한 SDK 대화 세션(`session=conversation_session`)을 전달하므로 하나의 메모리 대화 파일에 추가되며, 따라서 같은 `session.session_id`를 공유합니다. 이는 라이브 워크스페이스를 식별하지만 메모리 대화 ID로는 사용되지 않는 sandbox(`sandbox`)와는 다릅니다. 1단계는 sandbox 세션이 닫힐 때 누적된 대화를 확인하므로, 분리된 두 턴이 아니라 전체 교환에서 메모리를 추출할 수 있습니다.

여러 `Runner.run(...)` 호출이 하나의 메모리 대화가 되도록 하려면, 해당 호출들에 걸쳐 안정적인 식별자를 전달하세요. 메모리가 실행을 대화와 연결할 때는 다음 순서로 이를 확인합니다.

1. `Runner.run(...)`에 전달한 경우의 `conversation_id`
2. `SQLiteSession`과 같은 SDK `Session`을 전달한 경우의 `session.session_id`
3. 위 둘 다 없는 경우의 `RunConfig.group_id`
4. 안정적인 식별자가 없는 경우의 실행별 생성 ID

## 여러 에이전트의 메모리 분리를 위한 다른 레이아웃 사용

메모리 분리는 에이전트 이름이 아니라 `MemoryLayoutConfig`를 기준으로 합니다. 동일한 레이아웃과 동일한 메모리 대화 ID를 가진 에이전트는 하나의 메모리 대화와 하나의 통합 메모리를 공유합니다. 레이아웃이 다른 에이전트는 같은 sandbox 워크스페이스를 공유하더라도 별도의 rollout 파일, 원문 메모리, `MEMORY.md`, `memory_summary.md`를 유지합니다.

여러 에이전트가 하나의 sandbox를 공유하지만 메모리를 공유해서는 안 되는 경우에는 별도의 레이아웃을 사용하세요.

```python
from agents import SQLiteSession
from agents.sandbox import MemoryLayoutConfig, SandboxAgent
from agents.sandbox.capabilities import Filesystem, Memory, Shell

gtm_agent = SandboxAgent(
    name="GTM reviewer",
    instructions="Analyze GTM workspace data and write concise recommendations.",
    capabilities=[
        Memory(
            layout=MemoryLayoutConfig(
                memories_dir="memories/gtm",
                sessions_dir="sessions/gtm",
            )
        ),
        Filesystem(),
        Shell(),
    ],
)

engineering_agent = SandboxAgent(
    name="Engineering reviewer",
    instructions="Inspect engineering workspaces and summarize fixes and risks.",
    capabilities=[
        Memory(
            layout=MemoryLayoutConfig(
                memories_dir="memories/engineering",
                sessions_dir="sessions/engineering",
            )
        ),
        Filesystem(),
        Shell(),
    ],
)

gtm_session = SQLiteSession("gtm-q2-pipeline-review")
engineering_session = SQLiteSession("eng-invoice-test-fix")
```

이렇게 하면 GTM 분석이 엔지니어링 버그 수정 메모리에 통합되는 것을 방지하고, 그 반대도 방지할 수 있습니다.