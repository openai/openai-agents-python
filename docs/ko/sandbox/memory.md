---
search:
  exclude: true
---
# 에이전트 메모리

메모리는 향후 sandbox-agent 실행이 이전 실행에서 학습할 수 있게 합니다. 이는 메시지 기록을 저장하는 SDK의 대화형 [`Session`](../sessions/index.md) 메모리와는 별개입니다. 메모리는 이전 실행에서 얻은 교훈을 샌드박스 워크스페이스의 파일로 정제합니다.

!!! warning "베타 기능"

    샌드박스 에이전트는 베타 버전입니다. API, 기본값, 지원 기능의 세부 사항은 정식 출시 전에 변경될 수 있으며, 시간이 지나면서 더 고급 기능이 추가될 예정입니다.

메모리는 향후 실행에서 세 가지 비용을 줄일 수 있습니다.

1. 에이전트 비용: 에이전트가 워크플로를 완료하는 데 오랜 시간이 걸렸다면, 다음 실행에서는 탐색이 덜 필요해야 합니다. 이를 통해 토큰 사용량과 완료까지 걸리는 시간을 줄일 수 있습니다.
2. 사용자 비용: 사용자가 에이전트를 수정했거나 선호 사항을 표현했다면, 향후 실행에서 해당 피드백을 기억할 수 있습니다. 이를 통해 사람의 개입을 줄일 수 있습니다.
3. 컨텍스트 비용: 에이전트가 이전에 작업을 완료했고 사용자가 그 작업을 이어서 진행하려는 경우, 사용자가 이전 스레드를 찾거나 모든 컨텍스트를 다시 입력할 필요가 없어야 합니다. 이를 통해 작업 설명을 더 짧게 만들 수 있습니다.

버그를 수정하고, 메모리를 생성하고, 스냅샷을 재개한 뒤, 후속 검증 실행에서 해당 메모리를 사용하는 완전한 2회 실행 예제는 [examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py)를 참고하세요. 별도의 메모리 레이아웃을 사용하는 멀티턴, 멀티 에이전트 예제는 [examples/sandbox/memory_multi_agent_multiturn.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory_multi_agent_multiturn.py)를 참고하세요.

## 메모리 활성화

샌드박스 에이전트에 기능으로 `Memory()`를 추가합니다.

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

읽기가 활성화된 경우 `Memory()`에는 `Shell()`이 필요합니다. 이는 주입된 요약만으로 충분하지 않을 때 에이전트가 메모리 파일을 읽고 검색할 수 있게 합니다. 라이브 메모리 업데이트가 활성화된 경우(기본값), `Filesystem()`도 필요합니다. 이는 에이전트가 오래된 메모리를 발견하거나 사용자가 메모리 업데이트를 요청할 때 `memories/MEMORY.md`를 업데이트할 수 있게 합니다.

기본적으로 메모리 아티팩트는 샌드박스 워크스페이스의 `memories/` 아래에 저장됩니다. 나중 실행에서 이를 재사용하려면 동일한 라이브 샌드박스 세션을 유지하거나, 영구 저장된 세션 상태 또는 스냅샷에서 재개하여 구성된 memories 디렉터리 전체를 보존하고 재사용하세요. 새 빈 샌드박스는 빈 메모리로 시작합니다.

`Memory()`는 메모리 읽기와 생성을 모두 활성화합니다. 메모리를 읽어야 하지만 새 메모리를 생성해서는 안 되는 에이전트에는 `Memory(generate=None)`을 사용하세요. 예를 들어 내부 에이전트, 서브에이전트, 검사기, 또는 실행이 많은 신호를 추가하지 않는 일회성 도구 에이전트가 이에 해당합니다. 실행이 나중을 위한 메모리는 생성해야 하지만, 기존 메모리의 영향을 받는 것을 사용자가 원하지 않는 경우에는 `Memory(read=None)`을 사용하세요.

## 메모리 읽기

메모리 읽기는 점진적 공개 방식을 사용합니다. 실행 시작 시 SDK는 일반적으로 유용한 팁, 사용자 선호 사항, 사용 가능한 메모리에 대한 작은 요약(`memory_summary.md`)을 에이전트의 개발자 프롬프트에 주입합니다. 이를 통해 에이전트는 이전 작업이 관련될 수 있는지 판단하기에 충분한 컨텍스트를 얻습니다.

이전 작업이 관련 있어 보이면, 에이전트는 현재 작업의 키워드로 구성된 메모리 인덱스(`memories_dir` 아래의 `MEMORY.md`)를 검색합니다. 작업에 더 자세한 정보가 필요할 때만 구성된 `rollout_summaries/` 디렉터리 아래의 해당 이전 롤아웃 요약을 엽니다.

메모리는 오래될 수 있습니다. 에이전트는 메모리를 지침으로만 취급하고 현재 환경을 신뢰하도록 지시받습니다. 기본적으로 메모리 읽기에는 `live_update`가 활성화되어 있으므로, 에이전트가 오래된 메모리를 발견하면 동일한 실행에서 구성된 `MEMORY.md`를 업데이트할 수 있습니다. 에이전트가 메모리를 읽어야 하지만 실행 중에 수정해서는 안 되는 경우, 예를 들어 실행이 지연 시간에 민감한 경우에는 라이브 업데이트를 비활성화하세요.

## 메모리 생성

실행이 끝나면 샌드박스 런타임은 해당 실행 세그먼트를 대화 파일에 추가합니다. 누적된 대화 파일은 샌드박스 세션이 닫힐 때 처리됩니다.

메모리 생성에는 두 단계가 있습니다.

1. 1단계: 대화 추출. 메모리 생성 모델이 누적된 대화 파일 하나를 처리하고 대화 요약을 생성합니다. 시스템, 개발자, 추론 내용은 생략됩니다. 대화가 너무 길면 컨텍스트 창에 맞도록 잘리며, 시작과 끝은 보존됩니다. 또한 원문 메모리 추출도 생성합니다. 이는 2단계에서 통합할 수 있는 대화의 간결한 노트입니다.
2. 2단계: 레이아웃 통합. 통합 에이전트가 하나의 메모리 레이아웃에 대한 원문 메모리를 읽고, 더 많은 근거가 필요할 때 대화 요약을 연 다음, 패턴을 `MEMORY.md`와 `memory_summary.md`로 추출합니다.

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

`extra_prompt`를 사용하여 메모리 생성기에 사용 사례에서 가장 중요한 신호를 알려줄 수 있습니다. 예를 들어 GTM 에이전트의 경우 고객 및 회사 세부 정보가 해당됩니다.

최근 원문 메모리가 `max_raw_memories_for_consolidation`(기본값 256)을 초과하면, 2단계는 가장 최신 대화의 메모리만 유지하고 더 오래된 메모리는 제거합니다. 최신성은 대화가 마지막으로 업데이트된 시간을 기준으로 합니다. 이 망각 메커니즘은 메모리가 최신 환경을 반영하는 데 도움이 됩니다.

## 멀티턴 대화

멀티턴 샌드박스 채팅에는 일반 SDK `Session`을 동일한 라이브 샌드박스 세션과 함께 사용하세요.

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

두 실행은 동일한 SDK 대화 세션(`session=conversation_session`)을 전달하므로 같은 `session.session_id`를 공유하고, 따라서 하나의 메모리 대화 파일에 추가됩니다. 이는 라이브 워크스페이스를 식별하며 메모리 대화 ID로 사용되지 않는 샌드박스(`sandbox`)와 다릅니다. 1단계는 샌드박스 세션이 닫힐 때 누적된 대화를 보므로, 서로 분리된 두 턴이 아니라 전체 교환에서 메모리를 추출할 수 있습니다.

여러 `Runner.run(...)` 호출이 하나의 메모리 대화가 되도록 하려면 해당 호출들에 안정적인 식별자를 전달하세요. 메모리가 실행을 대화와 연결할 때는 다음 순서로 확인합니다.

1. `Runner.run(...)`에 전달한 경우 `conversation_id`
2. `SQLiteSession` 같은 SDK `Session`을 전달한 경우 `session.session_id`
3. 위 둘 중 어느 것도 없을 경우 `RunConfig.group_id`
4. 안정적인 식별자가 없을 경우 생성된 실행별 ID

## 서로 다른 에이전트의 메모리를 격리하기 위한 서로 다른 레이아웃 사용

메모리 격리는 에이전트 이름이 아니라 `MemoryLayoutConfig`를 기준으로 합니다. 동일한 레이아웃과 동일한 메모리 대화 ID를 가진 에이전트는 하나의 메모리 대화와 하나의 통합된 메모리를 공유합니다. 서로 다른 레이아웃을 가진 에이전트는 동일한 샌드박스 워크스페이스를 공유하더라도 별도의 롤아웃 파일, 원문 메모리, `MEMORY.md`, `memory_summary.md`를 유지합니다.

여러 에이전트가 하나의 샌드박스를 공유하지만 메모리는 공유해서는 안 되는 경우 별도의 레이아웃을 사용하세요.

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

이렇게 하면 GTM 분석이 엔지니어링 버그 수정 메모리로 통합되거나 그 반대가 되는 일을 방지할 수 있습니다.