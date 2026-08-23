---
search:
  exclude: true
---
# 도구

도구를 사용하면 에이전트가 데이터 가져오기, 코드 실행, 외부 API 호출, 컴퓨터 사용과 같은 작업을 수행할 수 있습니다. SDK는 다음 다섯 가지 카테고리를 지원합니다.

-   OpenAI 호스티드 툴: OpenAI 서버에서 모델을 위해 실행됩니다.
-   로컬/런타임 실행 도구: `ComputerTool` 및 `ApplyPatchTool`은 항상 사용자의 환경에서 실행되며, `ShellTool`는 로컬 또는 호스티드 컨테이너에서 실행할 수 있습니다.
-   `FunctionTool` 인스턴스: 모든 Python 함수를 도구로 래핑합니다.
-   Agents as tools: 전체 핸드오프 없이 에이전트를 호출 가능한 도구로 노출합니다.
-   실험적 기능: Codex 도구: 도구 호출을 통해 워크스페이스 범위의 Codex 작업을 실행합니다.

## 도구 유형 선택 {#choosing-a-tool-type}

이 페이지를 카탈로그로 활용한 다음, 제어하는 런타임에 해당하는 섹션으로 이동하세요.

| 원하는 작업 | 시작 위치 |
| --- | --- |
| OpenAI 관리 도구 사용(웹 검색, 파일 검색, Code Interpreter, 호스티드 MCP, 이미지 생성) | [호스티드 툴](#hosted-tools) |
| 도구 검색을 사용해 대규모 도구 범위를 런타임까지 지연 | [호스티드 도구 검색](#hosted-tool-search) |
| 생성된 JavaScript에서 여러 도구 호출 조정 | [프로그래밍 방식 도구 호출](#programmatic-tool-calling) |
| 자체 프로세스 또는 환경에서 도구 실행 | [로컬 런타임 도구](#local-runtime-tools) |
| Python 함수를 도구로 래핑 | [함수 도구](#function-tools) |
| 핸드오프 없이 한 에이전트가 다른 에이전트 호출 | [Agents as tools](#agents-as-tools) |
| 에이전트에서 워크스페이스 범위의 Codex 작업 실행 | [실험적 기능: Codex 도구](#experimental-codex-tool) |

## 호스티드 툴 {#hosted-tools}

OpenAI는 [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel]을 사용할 때 다음과 같은 기본 제공 도구를 제공합니다.

-   [`WebSearchTool`][agents.tool.WebSearchTool]를 사용하면 에이전트가 웹을 검색할 수 있습니다.
-   [`FileSearchTool`][agents.tool.FileSearchTool]를 사용하면 OpenAI 벡터 스토어에서 정보를 검색할 수 있습니다.
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool]를 사용하면 LLM이 샌드박스 환경에서 코드를 실행할 수 있습니다.
-   [`HostedMCPTool`][agents.tool.HostedMCPTool]는 원격 MCP 서버의 도구를 모델에 노출합니다.
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool]는 프롬프트로 이미지를 생성합니다.
-   [`ToolSearchTool`][agents.tool.ToolSearchTool]를 사용하면 모델이 지연된 도구, 네임스페이스 또는 호스티드 MCP 서버를 필요할 때 불러올 수 있습니다.
-   [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool]를 사용하면 모델이 생성된 JavaScript에서 사용 가능한 도구를 조정할 수 있습니다.

고급 호스티드 검색 옵션:

-   `FileSearchTool`는 `vector_store_ids` 및 `max_num_results` 외에도 `filters`, `ranking_options`, `include_search_results`를 지원합니다. `max_num_results`을 1에서 50 사이의 정수로 설정하세요. `None` 또는 0을 사용하면 공급자 기본값이 적용됩니다.
-   `WebSearchTool`는 `filters`, `user_location`, `search_context_size`을 지원합니다.

```python
from agents import Agent, FileSearchTool, Runner, WebSearchTool

agent = Agent(
    name="Assistant",
    tools=[
        WebSearchTool(),
        FileSearchTool(
            max_num_results=3,
            vector_store_ids=["VECTOR_STORE_ID"],
        ),
    ],
)

async def main():
    result = await Runner.run(agent, "Which coffee shop should I go to, taking into account my preferences and the weather today in SF?")
    print(result.final_output)
```

### 호스티드 도구 검색 {#hosted-tool-search}

도구 검색을 사용하면 OpenAI Responses 모델이 대규모 도구 범위의 로드를 런타임까지 지연하여 현재 턴에 필요한 하위 집합만 불러올 수 있습니다. 함수 도구, 네임스페이스 그룹 또는 호스티드 MCP 서버가 많을 때 모든 도구를 미리 노출하지 않고 도구 스키마 토큰을 줄이는 데 유용합니다.

에이전트를 빌드할 때 후보 도구가 이미 정해져 있다면 호스티드 도구 검색으로 시작하세요. 애플리케이션에서 무엇을 불러올지 동적으로 결정해야 하는 경우 Responses API는 클라이언트 실행 도구 검색도 지원하지만, 표준 `Runner`는 해당 모드를 자동으로 실행하지 않습니다.

```python
from typing import Annotated

from agents import Agent, Runner, ToolSearchTool, tool_namespace
from agents.decorators import tool


@tool(defer_loading=True)
def get_customer_profile(
    customer_id: Annotated[str, "The customer ID to look up."],
) -> str:
    """Fetch a CRM customer profile."""
    return f"profile for {customer_id}"


@tool(defer_loading=True)
def list_open_orders(
    customer_id: Annotated[str, "The customer ID to look up."],
) -> str:
    """List open orders for a customer."""
    return f"open orders for {customer_id}"


crm_tools = tool_namespace(
    name="crm",
    description="CRM tools for customer lookups.",
    tools=[get_customer_profile, list_open_orders],
)


agent = Agent(
    name="Operations assistant",
    model="gpt-5.6-sol",
    instructions="Load the crm namespace before using CRM tools.",
    tools=[*crm_tools, ToolSearchTool()],
)

result = await Runner.run(agent, "Look up customer_42 and list their open orders.")
print(result.final_output)
```

알아둘 사항:

-   호스티드 도구 검색은 OpenAI Responses 모델에서만 사용할 수 있습니다. 현재 Python SDK 지원 여부는 `openai>=2.25.0`에 따라 달라집니다.
-   에이전트에서 지연 로딩 범위를 구성할 때 `ToolSearchTool()`을 정확히 하나 추가하세요.
-   검색 가능한 범위에는 `@function_tool(defer_loading=True)`, `tool_namespace(name=..., description=..., tools=[...])`, `HostedMCPTool(tool_config={..., "defer_loading": True})`이 포함됩니다.
-   지연 로딩 함수 도구는 `ToolSearchTool()`과 함께 사용해야 합니다. 네임스페이스만 사용하는 설정에서는 모델이 필요할 때 올바른 그룹을 불러올 수 있도록 `ToolSearchTool()`도 사용할 수 있습니다.
-   `tool_namespace()`는 `FunctionTool` 인스턴스를 공유 네임스페이스 이름과 설명 아래에 그룹화합니다. `crm`, `billing`, `shipping`처럼 관련 도구가 많은 경우 일반적으로 가장 적합합니다.
-   OpenAI의 공식 권장 지침은 [가능하면 네임스페이스 사용](https://developers.openai.com/api/docs/guides/tools-tool-search#use-namespaces-where-possible)입니다.
-   가능하면 개별적으로 지연된 여러 함수보다 네임스페이스나 호스티드 MCP 서버를 우선 사용하세요. 일반적으로 모델에 더 나은 상위 수준 검색 범위를 제공하고 토큰을 더 많이 절약할 수 있습니다.
-   네임스페이스에는 즉시 사용 가능한 도구와 지연된 도구를 함께 포함할 수 있습니다. `defer_loading=True`이 없는 도구는 즉시 호출할 수 있으며, 같은 네임스페이스에 있는 지연된 도구는 도구 검색을 통해 불러옵니다.
-   일반적으로 각 네임스페이스를 비교적 작게 유지하며, 이상적으로는 함수 수를 10개 미만으로 제한하세요.
-   이름이 지정된 `tool_choice`은 단독 네임스페이스 이름이나 지연 전용 도구를 대상으로 지정할 수 없습니다. `auto`, `required` 또는 실제 최상위 호출 가능 도구 이름을 우선 사용하세요.
-   `ToolSearchTool(execution="client")`은 수동 Responses 오케스트레이션에 사용합니다. 모델이 클라이언트 실행 `tool_search_call`를 내보내면 표준 `Runner`는 대신 실행하지 않고 예외를 발생시킵니다.
-   도구 검색 활동은 전용 항목 및 이벤트 유형과 함께 [`RunResult.new_items`](results.md#new-items) 및 [`RunItemStreamEvent`](streaming.md#run-item-event-names)에 표시됩니다.
-   네임스페이스 로딩과 최상위 지연 도구를 모두 다루는 완전한 실행 가능 예제는 `examples/tools/tool_search.py`을 참고하세요.
-   공식 플랫폼 가이드: [도구 검색](https://developers.openai.com/api/docs/guides/tools-tool-search)

### 프로그래밍 방식 도구 호출 {#programmatic-tool-calling}

프로그래밍 방식 도구 호출을 사용하면 지원되는 OpenAI Responses 모델이 사용 가능한 도구를 호출하고, 출력을 결합하고, 하나의 결과를 모델에 반환하는 JavaScript를 생성할 수 있습니다. 모든 도구 호출 후 모델과의 왕복 없이 루프, 분기, 병렬 호출 또는 중간 계산을 활용하는 범위가 제한된 워크플로에 유용합니다.

생성된 프로그램은 새로운 호스티드 V8 환경에서 실행됩니다. Node.js API, 파일 시스템 또는 네트워크에 접근할 수 없으며 프로세스도 지속되지 않습니다. 프로그램은 명시적으로 허용한 도구하고만 상호작용할 수 있습니다.

```python
from pydantic import BaseModel

from agents import (
    Agent,
    ModelSettings,
    ProgrammaticToolCallingTool,
    Runner,
)
from agents.decorators import tool


class InventoryOutput(BaseModel):
    sku: str
    available_units: int


@tool(allowed_callers=["programmatic"])
def get_inventory(sku: str) -> InventoryOutput:
    return InventoryOutput(sku=sku, available_units=42)


agent = Agent(
    name="Inventory planner",
    model="gpt-5.6",
    model_settings=ModelSettings(tool_choice="programmatic_tool_calling"),
    tools=[get_inventory, ProgrammaticToolCallingTool()],
)

result = Runner.run_sync(agent, "Check inventory for desk-lamp and summarize it.")
print(result.final_output)
```

알아둘 사항:

-   프로그래밍 방식 도구 호출은 지원되는 OpenAI Responses 모델에서만 사용할 수 있습니다. `ProgrammaticToolCallingTool()` 및 `tool_choice="programmatic_tool_calling"`은 Chat Completions 모델과 Responses 이외의 백엔드에서 거부됩니다.
-   에이전트에 `ProgrammaticToolCallingTool()`을 최대 하나만 추가하세요. 또한 에이전트는 프로그래밍 방식으로 호출할 수 있는 도구를 하나 이상 노출하거나, 네임스페이스·지연 함수·지연된 호스티드 MCP 서버가 뒷받침하는 `ToolSearchTool()` 또는 불투명한 프롬프트 관리 도구 범위를 노출해야 합니다. 검색 가능한 범위가 없는 단독 `ToolSearchTool()`은 거부됩니다.
-   `allowed_callers`는 도구를 호출할 수 있는 방식을 제어합니다. 생략하면 모델의 직접 호출만 허용됩니다. 프로그램 전용 접근에는 `["programmatic"]`을 사용하고, 두 방식 모두 허용하려면 `["direct", "programmatic"]`를 사용하세요.
-   선택적으로 사용할 수 있는 SDK 도구 유형은 `FunctionTool`, `CustomTool`, `ShellTool`, `ApplyPatchTool`, `HostedMCPTool`, `CodeInterpreterTool`입니다. 함수, 사용자 지정, 셸 및 패치 적용 도구는 `allowed_callers`을 직접 노출합니다. 호스티드 MCP와 Code Interpreter의 경우 `tool_config` 내부에 `allowed_callers`를 설정하세요.
-   `@function_tool(allowed_callers=[...])`의 경우 Pydantic 모델, TypedDict 또는 데이터 클래스와 같은 구조화된 반환 어노테이션은 자동으로 엄격한 객체 출력 스키마가 되며, 반환 값은 프로그램에 반환되기 전에 해당 스키마에 따라 검증됩니다. 함수에 사용할 수 있는 어노테이션이 없으면 `output_type=...`를 사용하고, 엄격한 객체 스키마를 이미 가지고 있다면 저수준 우회 수단인 `output_json_schema={...}`을 사용하세요. `output_type`과 `output_json_schema`은 함께 사용할 수 없습니다. `str`, `Any`, `None` 반환 어노테이션은 출력 스키마를 생성하지 않습니다. 스키마가 적용되고 프로그램이 소유하는 호출의 경우 자유 형식 텍스트가 출력 스키마를 충족하지 않으므로 기본 실패 포매터가 비활성화됩니다. 따라서 스키마에 부합하는 JSON을 반환하는 사용자 지정 `failure_error_function`를 제공하지 않으면 핸들러 예외가 전파됩니다.
-   프로그램 소유 SDK 도구도 일반 Runner 수명 주기를 사용합니다. 도구 입출력 가드레일, 훅, 시간 제한, 동시성 제한, 승인, 세션 및 `RunState` 일시 중지/재개 동작이 계속 적용되며, SDK는 각 하위 호출과 프로그램 호출자 간의 관계를 유지합니다.
-   `ProgrammaticToolCallingTool()`가 있으면 프로그램 실행 전이라도 모델 요청 재시도에 더 엄격한 재실행 안전 경계가 적용됩니다. SDK는 이러한 요청에 대해 공급자 관리 재시도와 WebSocket 사전 이벤트 재시도를 비활성화합니다. Runner 재시도 정책은 공급자의 지침에서 재실행이 안전하다고 명시적으로 표시된 경우에만 재시도합니다. `retry_policies.network_error()`만으로는 이 경계를 재정의하지 않습니다.
-   승인에 민감하거나 영향이 큰 도구는 일반적으로 직접 호출로 유지하여 더 큰 프로그램의 일부가 되기 전에 각 작업을 사람이 검토할 수 있게 하는 편이 좋습니다. 프로그램 소유 호출이 승인을 위해 일시 중지되면 `RunState`을 통해 인터럽션(중단 처리)을 해결하고 평소처럼 원래 실행을 재개하세요.
-   프로그래밍 방식 도구 호출은 [호스티드 도구 검색](#hosted-tool-search)과 함께 사용할 수 있습니다. 모델은 생성된 프로그램이 지연된 도구를 호출하기 전에 해당 도구를 불러와야 합니다.
-   `program` 항목과 그에 속한 일반적인 프로그램 소유 하위 도구 호출은 [`ToolCallItem`][agents.items.ToolCallItem] 항목으로 표시됩니다. 이에 대응하는 `program_output`는 [`ToolCallOutputItem`][agents.items.ToolCallOutputItem]으로 표시됩니다. 호스티드 MCP 승인 요청과 도구 카탈로그는 대신 특수 MCP 항목과 스트림 이벤트를 사용합니다. 검사에 관한 자세한 내용은 [결과](results.md#new-items) 및 [스트리밍](streaming.md#run-item-event-names)을 참고하세요.
-   완전한 동시성 재고 계획 예제는 `examples/tools/programmatic_tool_calling.py`을 참고하세요.
-   공식 플랫폼 가이드: [프로그래밍 방식 도구 호출](https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling)

### 호스티드 컨테이너 셸 및 스킬 {#hosted-container-shell-skills}

`ShellTool`는 OpenAI 호스티드 컨테이너 실행도 지원합니다. 로컬 런타임 대신 관리형 컨테이너에서 모델이 셸 명령을 실행하도록 하려면 이 모드를 사용하세요.

```python
from agents import Agent, Runner, ShellTool, ShellToolSkillReference

csv_skill: ShellToolSkillReference = {
    "type": "skill_reference",
    "skill_id": "skill_698bbe879adc81918725cbc69dcae7960bc5613dadaed377",
    "version": "1",
}

agent = Agent(
    name="Container shell agent",
    model="gpt-5.6-sol",
    instructions="Use the mounted skill when helpful.",
    tools=[
        ShellTool(
            environment={
                "type": "container_auto",
                "network_policy": {"type": "disabled"},
                "skills": [csv_skill],
            }
        )
    ],
)

result = await Runner.run(
    agent,
    "Use the configured skill to analyze CSV files in /mnt/data and summarize totals by region.",
)
print(result.final_output)
```

이후 실행에서 기존 컨테이너를 재사용하려면 `environment={"type": "container_reference", "container_id": "cntr_..."}`을 설정하세요.

알아둘 사항:

-   호스티드 셸은 Responses API 셸 도구를 통해 사용할 수 있습니다.
-   `container_auto`는 요청을 위한 컨테이너를 프로비저닝하고, `container_reference`는 기존 컨테이너를 재사용합니다.
-   `container_auto`에는 `file_ids` 및 `memory_limit`도 포함할 수 있습니다.
-   `environment.skills`는 스킬 참조와 인라인 스킬 번들을 허용합니다.
-   호스티드 환경에서는 `ShellTool`에 `executor`, `needs_approval`, `on_approval`를 설정하지 마세요.
-   `network_policy`는 `disabled` 및 `allowlist` 모드를 지원합니다.
-   허용 목록 모드에서는 `network_policy.domain_secrets`이 이름을 기준으로 도메인 범위의 시크릿을 주입할 수 있습니다.
-   완전한 예제는 `examples/tools/container_shell_skill_reference.py` 및 `examples/tools/container_shell_inline_skill.py`를 참고하세요.
-   OpenAI 플랫폼 가이드: [셸](https://platform.openai.com/docs/guides/tools-shell) 및 [스킬](https://platform.openai.com/docs/guides/tools-skills)

## 로컬 런타임 도구 {#local-runtime-tools}

로컬 런타임 도구는 모델 응답 자체의 외부에서 실행됩니다. 모델이 호출 시점을 결정하지만, 실제 작업은 애플리케이션 또는 구성된 실행 환경에서 수행합니다.

`ComputerTool` 및 `ApplyPatchTool`에는 항상 사용자가 제공하는 로컬 구현이 필요합니다. `ShellTool`는 두 모드를 모두 지원합니다. 관리형 실행을 원하면 위의 호스티드 컨테이너 구성을 사용하고, 자체 프로세스에서 명령을 실행하려면 아래의 로컬 런타임 구성을 사용하세요.

로컬 런타임 도구에는 다음 구현을 제공해야 합니다.

-   [`ComputerTool`][agents.tool.ComputerTool]: GUI/브라우저 자동화를 활성화하려면 [`Computer`][agents.computer.Computer] 또는 [`AsyncComputer`][agents.computer.AsyncComputer] 인터페이스를 구현하세요.
-   [`ShellTool`][agents.tool.ShellTool]: 로컬 실행과 호스티드 컨테이너 실행 모두를 위한 최신 셸 도구입니다.
-   [`LocalShellTool`][agents.tool.LocalShellTool]: 레거시 로컬 셸 통합입니다.
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]: 로컬에서 diff를 적용하려면 [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor]를 구현하세요.
-   로컬 셸 스킬은 `ShellTool(environment={"type": "local", "skills": [...]})`과 함께 사용할 수 있습니다.

셸 작업 시간 제한은 유한한 시간 제한에 양의 정수 밀리초를 사용합니다. 0은 실행기 구현 간에 이식 가능한 의미를 갖지 않으므로 SDK는 로컬 `ShellTool` 실행기를 호출하기 전에 `0`과 `None`를 모두 명시적 시간 제한 없음으로 처리합니다. 그 밖의 값은 실행기 호출 전에 거부됩니다. 이는 시간 제한 필드에만 해당합니다. `max_output_length=0`는 캡처된 빈 출력 요청으로 계속 지원됩니다.

### ComputerTool과 Responses 컴퓨터 도구 {#computertool-and-the-responses-computer-tool}

`ComputerTool`는 여전히 로컬 하네스입니다. 사용자가 [`Computer`][agents.computer.Computer] 또는 [`AsyncComputer`][agents.computer.AsyncComputer] 구현을 제공하면 SDK가 해당 하네스를 OpenAI Responses API 컴퓨터 인터페이스에 매핑합니다.

명시적인 [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) 요청의 경우 SDK는 GA 기본 제공 도구 페이로드 `{"type": "computer"}`를 전송합니다. 이전 `computer-use-preview` 모델에 대한 요청의 경우 SDK는 계속해서 프리뷰 페이로드 `{"type": "computer_use_preview", "environment": ..., "display_width": ..., "display_height": ...}`을 전송합니다. 이는 OpenAI의 [컴퓨터 사용 가이드](https://developers.openai.com/api/docs/guides/tools-computer-use/)에 설명된 플랫폼 마이그레이션을 반영합니다.

-   모델: `computer-use-preview` -> `gpt-5.5`
-   도구 선택자: `computer_use_preview` -> `computer`
-   컴퓨터 호출 형식: `computer_call`당 하나의 `action` -> `computer_call`의 일괄 처리된 `actions[]`
-   잘림: 프리뷰 경로에서는 `ModelSettings(truncation="auto")` 필요 -> GA 경로에서는 불필요

SDK는 실제 Responses 요청의 유효 모델을 기준으로 해당 전송 형식을 선택합니다. 프롬프트 템플릿을 사용하고 프롬프트에서 모델을 소유하므로 요청에서 `model`을 생략한 경우, `model="gpt-5.5"`를 명시적으로 유지하거나 `ModelSettings(tool_choice="computer")` 또는 `ModelSettings(tool_choice="computer_use")`로 GA 선택자를 강제하지 않는 한 SDK는 프리뷰 호환 컴퓨터 페이로드를 유지합니다.

[`ComputerTool`][agents.tool.ComputerTool]가 있으면 `tool_choice="computer"`, `"computer_use"`, `"computer_use_preview"`이 모두 허용되며 유효 요청 모델과 일치하는 기본 제공 선택자로 정규화됩니다. `ComputerTool`가 없으면 이러한 문자열은 계속 일반 함수 이름처럼 동작합니다.

이 차이는 `ComputerTool`이 [`ComputerProvider`][agents.tool.ComputerProvider] 팩터리를 기반으로 할 때 중요합니다. GA `computer` 페이로드는 직렬화 시점에 `environment`이나 크기가 필요하지 않으므로 팩터리가 `Computer` 또는 `AsyncComputer` 인스턴스를 생성하기 전에 직렬화할 수 있습니다. 프리뷰 호환 직렬화에는 SDK가 `environment`, `display_width`, `display_height`을 전송할 수 있도록 확인된 `Computer` 또는 `AsyncComputer` 인스턴스가 여전히 필요합니다.

런타임에서 두 경로는 계속 동일한 로컬 하네스를 사용합니다. 프리뷰 응답은 단일 `action`를 포함하는 `computer_call` 항목을 내보냅니다. `gpt-5.5`은 일괄 처리된 `actions[]`를 내보낼 수 있으며, SDK는 `computer_call_output` 스크린샷 항목을 생성하기 전에 이를 순서대로 실행합니다. 실행 가능한 Playwright 기반 하네스는 `examples/tools/computer_use.py`을 참고하세요.

```python
from agents import Agent, ApplyPatchTool, ShellTool
from agents.computer import AsyncComputer
from agents.editor import ApplyPatchResult, ApplyPatchOperation, ApplyPatchEditor


class NoopComputer(AsyncComputer):
    environment = "browser"
    dimensions = (1024, 768)
    async def screenshot(self): return ""
    async def click(self, x, y, button): ...
    async def double_click(self, x, y): ...
    async def scroll(self, x, y, scroll_x, scroll_y): ...
    async def type(self, text): ...
    async def wait(self): ...
    async def move(self, x, y): ...
    async def keypress(self, keys): ...
    async def drag(self, path): ...


class NoopEditor(ApplyPatchEditor):
    async def create_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")
    async def update_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")
    async def delete_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")


async def run_shell(request):
    return "shell output"


agent = Agent(
    name="Local tools agent",
    tools=[
        ShellTool(executor=run_shell),
        ApplyPatchTool(editor=NoopEditor()),
        # ComputerTool expects a Computer/AsyncComputer implementation; omitted here for brevity.
    ],
)
```

## 함수 도구 {#function-tools}

모든 Python 함수를 도구로 사용할 수 있습니다. Agents SDK가 도구를 자동으로 설정합니다.

-   도구 이름은 Python 함수 이름이 됩니다. 또는 이름을 직접 제공할 수 있습니다.
-   도구 설명은 함수의 docstring에서 가져옵니다. 또는 설명을 직접 제공할 수 있습니다.
-   함수 입력 스키마는 함수 인수에서 자동으로 생성됩니다.
-   비활성화하지 않는 한 각 입력의 설명은 함수의 docstring에서 가져옵니다.

`@tool`로 생성한 도구는 읽기 전용 `__wrapped__` 속성을 통해 원래 Python 호출 가능 객체를 노출합니다. 이는 검사와 테스트에 유용하지만, 이를 직접 호출하면 스키마 검증, 컨텍스트 주입, 가드레일, 시간 제한, 실패 처리, 트레이싱을 포함한 도구 런타임 파이프라인을 우회합니다. 직접 만든 `FunctionTool` 인스턴스는 `__wrapped__`을 노출하지 않습니다.

함수 시그니처를 추출하기 위해 Python의 `inspect` 모듈을 사용하고, docstring 구문 분석에는 [`griffe`](https://mkdocstrings.github.io/griffe/), 스키마 생성에는 `pydantic`을 사용합니다.

OpenAI Responses 모델을 사용하는 경우 `@function_tool(defer_loading=True)`는 `ToolSearchTool()`가 불러올 때까지 함수 도구를 숨깁니다. [`tool_namespace()`][agents.tool.tool_namespace]를 사용하여 관련 함수 도구를 그룹화할 수도 있습니다. 전체 설정과 제약 조건은 [호스티드 도구 검색](#hosted-tool-search)을 참고하세요.

```python
import json

from typing_extensions import TypedDict, Any

from agents import Agent, FunctionTool, RunContextWrapper
from agents.decorators import tool


class Location(TypedDict):
    lat: float
    long: float

@tool  # (1)!
async def fetch_weather(location: Location) -> str:
    # (2)!
    """Fetch the weather for a given location.

    Args:
        location: The location to fetch the weather for.
    """
    # In real life, we'd fetch the weather from a weather API
    return "sunny"


@tool(name_override="fetch_data")  # (3)!
def read_file(ctx: RunContextWrapper[Any], path: str, directory: str | None = None) -> str:
    """Read the contents of a file.

    Args:
        path: The path to the file to read.
        directory: The directory to read the file from.
    """
    # In real life, we'd read the file from the file system
    return "<file contents>"


agent = Agent(
    name="Assistant",
    tools=[fetch_weather, read_file],  # (4)!
)

for tool in agent.tools:
    if isinstance(tool, FunctionTool):
        print(tool.name)
        print(tool.description)
        print(json.dumps(tool.params_json_schema, indent=2))
        print()

```

1.  모든 Python 유형을 함수의 인수로 사용할 수 있으며, 함수는 동기 또는 비동기일 수 있습니다.
2.  docstring이 있으면 설명과 인수 설명을 가져오는 데 사용됩니다.
3.  함수는 선택적으로 실행 컨텍스트를 첫 번째 인수로 받을 수 있습니다. 도구 이름, 설명, 사용할 docstring 스타일 등의 재정의도 설정할 수 있습니다.
4.  데코레이트된 함수를 도구 목록에 전달할 수 있습니다.

??? note "출력 펼쳐 보기"

    ```
    fetch_weather
    Fetch the weather for a given location.
    {
    "$defs": {
      "Location": {
        "properties": {
          "lat": {
            "title": "Lat",
            "type": "number"
          },
          "long": {
            "title": "Long",
            "type": "number"
          }
        },
        "required": [
          "lat",
          "long"
        ],
        "title": "Location",
        "type": "object"
      }
    },
    "properties": {
      "location": {
        "$ref": "#/$defs/Location",
        "description": "The location to fetch the weather for."
      }
    },
    "required": [
      "location"
    ],
    "title": "fetch_weather_args",
    "type": "object"
    }

    fetch_data
    Read the contents of a file.
    {
    "properties": {
      "path": {
        "description": "The path to the file to read.",
        "title": "Path",
        "type": "string"
      },
      "directory": {
        "anyOf": [
          {
            "type": "string"
          },
          {
            "type": "null"
          }
        ],
        "default": null,
        "description": "The directory to read the file from.",
        "title": "Directory"
      }
    },
    "required": [
      "path"
    ],
    "title": "fetch_data_args",
    "type": "object"
    }
    ```

### 함수 도구에서 이미지 또는 파일 반환 {#returning-images-or-files-from-function-tools}

텍스트 출력뿐 아니라 하나 이상의 이미지나 파일을 함수 도구의 출력으로 반환할 수 있습니다. 이를 위해 다음 중 하나를 반환할 수 있습니다.

-   이미지: [`ToolOutputImage`][agents.tool.ToolOutputImage] 또는 TypedDict 버전인 [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]
-   파일: [`ToolOutputFileContent`][agents.tool.ToolOutputFileContent] 또는 TypedDict 버전인 [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]
-   텍스트: 문자열, 문자열로 변환 가능한 객체 또는 [`ToolOutputText`][agents.tool.ToolOutputText] 또는 TypedDict 버전인 [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]

### 사용자 지정 함수 도구 {#custom-function-tools}

Python 함수를 도구로 사용하지 않으려는 경우도 있습니다. 원하는 경우 [`FunctionTool`][agents.tool.FunctionTool]을 직접 생성할 수 있습니다. 다음 항목을 제공해야 합니다.

-   `name`
-   `description`
-   인수의 JSON 스키마인 `params_json_schema`
-   [`ToolContext`][agents.tool_context.ToolContext]와 JSON 문자열 형식의 인수를 받아 도구 출력(예: 텍스트, 구조화된 도구 출력 객체 또는 출력 목록)을 반환하는 비동기 함수인 `on_invoke_tool`

```python
from typing import Any

from pydantic import BaseModel

from agents import RunContextWrapper, FunctionTool



def do_some_work(data: str) -> str:
    return "done"


class FunctionArgs(BaseModel):
    username: str
    age: int


async def run_function(ctx: RunContextWrapper[Any], args: str) -> str:
    parsed = FunctionArgs.model_validate_json(args)
    return do_some_work(data=f"{parsed.username} is {parsed.age} years old")


tool = FunctionTool(
    name="process_user",
    description="Processes extracted user data",
    params_json_schema=FunctionArgs.model_json_schema(),
    on_invoke_tool=run_function,
)
```

### 자동 인수 및 docstring 구문 분석 {#automatic-argument-and-docstring-parsing}

앞서 설명한 것처럼 함수 시그니처를 자동으로 구문 분석하여 도구 스키마를 추출하고, docstring을 구문 분석하여 도구와 개별 인수의 설명을 추출합니다. 다음 사항을 참고하세요.

1. 시그니처 구문 분석은 `inspect` 모듈을 통해 수행됩니다. 타입 어노테이션을 사용해 인수 유형을 파악하고 전체 스키마를 나타내는 Pydantic 모델을 동적으로 빌드합니다. Python 기본 타입, Pydantic 모델, TypedDict 등 대부분의 유형을 지원합니다.
2. docstring 구문 분석에는 `griffe`을 사용합니다. 지원되는 docstring 형식은 `google`, `sphinx`, `numpy`입니다. docstring 형식을 자동으로 감지하려고 시도하지만 최선형 방식이므로 `function_tool`를 호출할 때 명시적으로 설정할 수 있습니다. `use_docstring_info`를 `False`으로 설정하여 docstring 구문 분석을 비활성화할 수도 있습니다. Google 스타일 docstring의 경우 파서는 요약 텍스트 바로 뒤에 빈 줄 없이 이어지는 `Args:`, `Arguments:`, `Params:`, `Parameters:` 섹션도 허용합니다.

스키마 추출 코드는 [`agents.function_schema`][]에 있습니다.

### Pydantic Field를 사용한 인수 제약 및 설명 {#constraining-and-describing-arguments-with-pydantic-field}

Pydantic의 [`Field`](https://docs.pydantic.dev/latest/concepts/fields/)를 사용하여 도구 인수에 제약 조건(예: 숫자의 최솟값/최댓값, 문자열의 길이 또는 패턴)과 설명을 추가할 수 있습니다. Pydantic과 마찬가지로 기본값 기반 형식(`arg: int = Field(..., ge=1)`)과 `Annotated` 형식(`arg: Annotated[int, Field(..., ge=1)]`)을 모두 지원합니다. 생성된 JSON 스키마와 검증에는 이러한 제약 조건이 포함됩니다.

```python
from typing import Annotated
from pydantic import Field
from agents.decorators import tool

# Default-based form
@tool
def score_a(score: int = Field(..., ge=0, le=100, description="Score from 0 to 100")) -> str:
    return f"Score recorded: {score}"

# Annotated form
@tool
def score_b(score: Annotated[int, Field(..., ge=0, le=100, description="Score from 0 to 100")]) -> str:
    return f"Score recorded: {score}"
```

### 함수 도구 시간 제한 {#function-tool-timeouts}

`@function_tool(timeout=...)`을 사용하여 비동기 함수 도구의 호출별 시간 제한을 설정할 수 있습니다.

```python
import asyncio
from agents import Agent
from agents.decorators import tool


@tool(timeout=2.0)
async def slow_lookup(query: str) -> str:
    await asyncio.sleep(10)
    return f"Result for {query}"


agent = Agent(
    name="Timeout demo",
    instructions="Use tools when helpful.",
    tools=[slow_lookup],
)
```

시간 제한에 도달하면 기본 동작은 `timeout_behavior="error_as_result"`이며, 모델에 표시되는 시간 초과 메시지(예: `Tool 'slow_lookup' timed out after 2 seconds.`)를 전송합니다.

시간 초과 처리를 제어할 수 있습니다.

-   `timeout_behavior="error_as_result"`(기본값): 모델이 복구할 수 있도록 시간 초과 메시지를 반환합니다.
-   `timeout_behavior="raise_exception"`: [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError]를 발생시키고 실행을 실패 처리합니다.
-   `error_as_result`을 사용할 때 `timeout_error_function=...`로 시간 초과 메시지를 사용자 지정합니다.

```python
import asyncio
from agents import Agent, Runner, ToolTimeoutError
from agents.decorators import tool


@tool(timeout=1.5, timeout_behavior="raise_exception")
async def slow_tool() -> str:
    await asyncio.sleep(5)
    return "done"


agent = Agent(name="Timeout hard-fail", tools=[slow_tool])

try:
    await Runner.run(agent, "Run the tool")
except ToolTimeoutError as e:
    print(f"{e.tool_name} timed out in {e.timeout_seconds} seconds")
```

!!! note

    시간 제한 구성은 비동기 `@function_tool` 핸들러에서만 지원됩니다.

### 함수 도구 오류 처리 {#handling-errors-in-function-tools}

`@function_tool`를 통해 함수 도구를 생성할 때 `failure_error_function`을 전달할 수 있습니다. 이는 도구 호출이 중단되는 경우 LLM에 오류 응답을 제공하는 함수입니다.

-   기본적으로, 즉 아무것도 전달하지 않으면 오류가 발생했음을 LLM에 알리는 `default_tool_error_function`이 실행됩니다.
-   자체 오류 함수를 전달하면 해당 함수가 대신 실행되고 응답이 LLM에 전송됩니다.
-   `None`을 명시적으로 전달하면 모든 도구 호출 오류가 다시 발생하여 직접 처리할 수 있습니다. 모델이 유효하지 않은 JSON을 생성한 경우 `ModelBehaviorError`, 코드 실행이 중단된 경우 `UserError` 등이 발생할 수 있습니다.

```python
from agents import RunContextWrapper
from agents.decorators import tool
from typing import Any

def my_custom_error_function(context: RunContextWrapper[Any], error: Exception) -> str:
    """A custom function to provide a user-friendly error message."""
    print(f"A tool call failed with the following error: {error}")
    return "An internal server error occurred. Please try again later."

@tool(failure_error_function=my_custom_error_function)
def get_user_profile(user_id: str) -> str:
    """Fetches a user profile from a mock API.
     This function demonstrates a 'flaky' or failing API call.
    """
    if user_id == "user_123":
        return "User profile for user_123 successfully retrieved."
    else:
        raise ValueError(f"Could not retrieve profile for user_id: {user_id}. API returned an error.")

```

`FunctionTool` 객체를 수동으로 생성하는 경우 `on_invoke_tool` 함수 내부에서 오류를 처리해야 합니다.

## Agents as tools {#agents-as-tools}

일부 워크플로에서는 제어를 핸드오프하는 대신 중앙 에이전트가 특화된 에이전트 네트워크를 오케스트레이션하도록 할 수 있습니다. 에이전트를 도구로 모델링하여 이를 구현할 수 있습니다.

```python
import asyncio

from agents import Agent, Runner

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You translate the user's message to Spanish",
)

french_agent = Agent(
    name="French agent",
    instructions="You translate the user's message to French",
)

orchestrator_agent = Agent(
    name="orchestrator_agent",
    instructions=(
        "You are a translation agent. You use the tools given to you to translate. "
        "If asked for multiple translations, you call the relevant tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="translate_to_spanish",
            tool_description="Translate the user's message to Spanish",
        ),
        french_agent.as_tool(
            tool_name="translate_to_french",
            tool_description="Translate the user's message to French",
        ),
    ],
)

async def main():
    result = await Runner.run(orchestrator_agent, input="Say 'Hello, how are you?' in Spanish.")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

### 도구 에이전트 사용자 지정 {#customizing-tool-agents}

`agent.as_tool`은 에이전트를 도구로 변환하는 편의 메서드입니다. `max_turns`, `run_config`, `hooks`, `previous_response_id`, `conversation_id`, `session`, `needs_approval`과 같은 일반적인 런타임 옵션을 지원합니다. 또한 `parameters`, `input_builder`, `include_input_schema`을 통한 구조화된 입력도 지원합니다.

상태 옵션은 도구 호출로 시작되는 중첩 에이전트 실행을 구성합니다. 상위 실행의 대화 상태는 자동으로 상속되지 않습니다. 상위 실행과 중첩 실행 간에 클라이언트 관리 기록을 공유하려면 동일한 `session`를 두 실행 모두에 명시적으로 전달하세요. `Runner.run`와 마찬가지로 중첩 실행에는 클라이언트 관리 `session` 또는 `previous_response_id`이나 `conversation_id`을 통한 서버 관리 연속 처리 중 하나의 상태 전략을 선택하세요.

```python
from agents.decorators import tool


@tool
async def run_my_agent() -> str:
    """A tool that runs the agent with custom configs"""

    agent = Agent(name="My agent", instructions="...")

    result = await Runner.run(
        agent,
        input="...",
        max_turns=5,
        run_config=...
    )

    return str(result.final_output)
```

### 도구 에이전트의 구조화된 입력 {#structured-input-for-tool-agents}

기본적으로 `Agent.as_tool()`는 하나의 문자열 필드 `input`(`{"input": "..."}`)이 있는 객체를 예상하지만, Pydantic 모델 유형 또는 데이터 클래스 유형인 `parameters`를 전달하여 구조화된 스키마를 노출할 수 있습니다.

추가 옵션:

- `include_input_schema=True`은 생성된 중첩 입력에 전체 JSON Schema를 포함합니다.
- `input_builder=...`를 사용하면 구조화된 도구 인수를 중첩 에이전트 입력으로 변환하는 방식을 완전히 사용자 지정할 수 있습니다.
- `RunContextWrapper.tool_input`에는 중첩 실행 컨텍스트 내부에서 구문 분석된 구조화 페이로드가 포함됩니다.

```python
from pydantic import BaseModel, Field


class TranslationInput(BaseModel):
    text: str = Field(description="Text to translate.")
    source: str = Field(description="Source language.")
    target: str = Field(description="Target language.")


translator_tool = translator_agent.as_tool(
    tool_name="translate_text",
    tool_description="Translate text between languages.",
    parameters=TranslationInput,
    include_input_schema=True,
)
```

완전한 실행 가능 예제는 `examples/agent_patterns/agents_as_tools_structured.py`을 참고하세요.

### 도구 에이전트의 승인 게이트 {#approval-gates-for-tool-agents}

`Agent.as_tool(..., needs_approval=...)`은 `function_tool`과 동일한 승인 흐름을 사용합니다. 승인이 필요하면 실행이 일시 중지되고 대기 중인 항목이 `result.interruptions`에 표시됩니다. 그런 다음 `result.to_state()`을 사용하고 `state.approve(...)` 또는 `state.reject(...)`를 호출한 후 재개하세요. 전체 일시 중지/재개 패턴은 [휴먼인더루프 (HITL) 가이드](human_in_the_loop.md)를 참고하세요.

### 사용자 지정 출력 추출 {#custom-output-extraction}

특정한 경우 중앙 에이전트에 반환하기 전에 도구 에이전트의 출력을 수정할 수 있습니다. 다음과 같은 경우에 유용합니다.

-   하위 에이전트의 채팅 기록에서 특정 정보(예: JSON 페이로드) 추출
-   에이전트의 최종 답변 변환 또는 재구성(예: Markdown을 일반 텍스트나 CSV로 변환)
-   에이전트의 응답이 누락되었거나 형식이 잘못된 경우 출력 검증 또는 대체 값 제공

`as_tool` 메서드에 `custom_output_extractor` 인수를 제공하여 이를 구현할 수 있습니다.

```python
async def extract_json_payload(run_result: RunResult) -> str:
    # Scan the agent’s outputs in reverse order until we find a JSON-like message from a tool call.
    for item in reversed(run_result.new_items):
        if isinstance(item, ToolCallOutputItem) and item.output.strip().startswith("{"):
            return item.output.strip()
    # Fallback to an empty JSON object if nothing was found
    return "{}"


json_tool = data_agent.as_tool(
    tool_name="get_data_json",
    tool_description="Run the data agent and return only its JSON payload",
    custom_output_extractor=extract_json_payload,
)
```

사용자 지정 추출기 내부에서 중첩된 [`RunResult`][agents.result.RunResult]는 [`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation]도 노출합니다. 중첩 결과를 후처리하는 동안 외부 도구 이름, 호출 ID 또는 raw 인수가 필요할 때 유용합니다. [결과 가이드](results.md#agent-as-tool-metadata)를 참고하세요.

### 중첩 에이전트 실행 스트리밍 {#streaming-nested-agent-runs}

스트림이 완료되면 최종 출력을 반환하면서 중첩 에이전트가 내보내는 스트리밍 이벤트를 수신하려면 `as_tool`에 `on_stream` 콜백을 전달하세요.

```python
from agents import AgentToolStreamEvent


async def handle_stream(event: AgentToolStreamEvent) -> None:
    # Inspect the underlying StreamEvent along with agent metadata.
    print(f"[stream] {event['agent'].name} :: {event['event'].type}")


billing_agent_tool = billing_agent.as_tool(
    tool_name="billing_helper",
    tool_description="Answer billing questions.",
    on_stream=handle_stream,  # Can be sync or async.
)
```

예상 동작:

- 이벤트 유형은 `StreamEvent["type"]`와 동일합니다. `raw_response_event`, `run_item_stream_event`, `agent_updated_stream_event`
- `on_stream`을 제공하면 중첩 에이전트가 자동으로 스트리밍 모드에서 실행되며, 최종 출력을 반환하기 전에 스트림을 모두 소비합니다.
- 핸들러는 동기 또는 비동기일 수 있으며, 각 이벤트는 도착하는 순서대로 전달됩니다.
- 모델 도구 호출을 통해 도구가 호출되면 `tool_call`가 존재합니다. 직접 호출에서는 `None`일 수 있습니다.
- 완전한 실행 가능 샘플은 `examples/agent_patterns/agents_as_tools_streaming.py`을 참고하세요.

### 조건부 도구 활성화 {#conditional-tool-enabling}

`is_enabled` 매개변수를 사용하여 런타임에 에이전트 도구를 조건부로 활성화하거나 비활성화할 수 있습니다. 이를 통해 컨텍스트, 사용자 기본 설정 또는 런타임 조건을 기준으로 LLM에서 사용할 수 있는 도구를 동적으로 필터링할 수 있습니다.

```python
import asyncio
from agents import Agent, AgentBase, Runner, RunContextWrapper
from pydantic import BaseModel

class LanguageContext(BaseModel):
    language_preference: str = "french_spanish"

def french_enabled(ctx: RunContextWrapper[LanguageContext], agent: AgentBase) -> bool:
    """Enable French for French+Spanish preference."""
    return ctx.context.language_preference == "french_spanish"

# Create specialized agents
spanish_agent = Agent(
    name="spanish_agent",
    instructions="You respond in Spanish. Always reply to the user's question in Spanish.",
)

french_agent = Agent(
    name="french_agent",
    instructions="You respond in French. Always reply to the user's question in French.",
)

# Create orchestrator with conditional tools
orchestrator = Agent(
    name="orchestrator",
    instructions=(
        "You are a multilingual assistant. You use the tools given to you to respond to users. "
        "You must call ALL available tools to provide responses in different languages. "
        "You never respond in languages yourself, you always use the provided tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="respond_spanish",
            tool_description="Respond to the user's question in Spanish",
            is_enabled=True,  # Always enabled
        ),
        french_agent.as_tool(
            tool_name="respond_french",
            tool_description="Respond to the user's question in French",
            is_enabled=french_enabled,
        ),
    ],
)

async def main():
    context = LanguageContext(language_preference="french_spanish")
    result = await Runner.run(orchestrator, "How are you?", context=context)
    print(result.final_output)

asyncio.run(main())
```

`is_enabled` 매개변수는 다음을 허용합니다.

-   **불리언 값**: `True`(항상 활성화) 또는 `False`(항상 비활성화)
-   **호출 가능 함수**: `(context, agent)`을 받아 불리언 값을 반환하는 함수
-   **비동기 함수**: 복잡한 조건부 로직을 위한 비동기 함수

비활성화된 도구는 런타임에 LLM에서 완전히 숨겨지므로 다음 용도에 유용합니다.

-   사용자 권한에 따른 기능 게이팅
-   환경별 도구 가용성(개발 환경과 프로덕션 환경)
-   다양한 도구 구성의 A/B 테스트
-   런타임 상태에 따른 동적 도구 필터링

## 실험적 기능: Codex 도구 {#experimental-codex-tool}

`codex_tool`는 Codex CLI를 래핑하여 에이전트가 도구 호출 중에 워크스페이스 범위의 작업(셸, 파일 편집, MCP 도구)을 실행할 수 있게 합니다. 이 인터페이스는 실험적이며 변경될 수 있습니다.

기본 에이전트가 현재 실행을 벗어나지 않고 범위가 제한된 워크스페이스 작업을 Codex에 위임하도록 하려면 사용하세요. 기본 도구 이름은 `codex`입니다. 사용자 지정 이름을 설정하는 경우 `codex`이거나 `codex_`로 시작해야 합니다. 에이전트에 여러 Codex 도구가 포함된 경우 각 도구에 고유한 이름을 사용해야 합니다.

```python
from agents import Agent
from agents.extensions.experimental.codex import ThreadOptions, TurnOptions, codex_tool

agent = Agent(
    name="Codex Agent",
    instructions="Use the codex tool to inspect the workspace and answer the question.",
    tools=[
        codex_tool(
            sandbox_mode="workspace-write",
            working_directory="/path/to/repo",
            default_thread_options=ThreadOptions(
                model="gpt-5.5",
                model_reasoning_effort="low",
                network_access_enabled=True,
                web_search_mode="disabled",
                approval_policy="never",
            ),
            default_turn_options=TurnOptions(
                idle_timeout_seconds=60,
            ),
            persist_session=True,
        )
    ],
)
```

다음 옵션 그룹부터 시작하세요.

-   실행 범위: `sandbox_mode` 및 `working_directory`은 Codex가 작업할 수 있는 위치를 정의합니다. 두 옵션을 함께 사용하고 작업 디렉터리가 Git 저장소 내부에 없으면 `skip_git_repo_check=True`을 설정하세요.
-   스레드 기본값: `default_thread_options=ThreadOptions(...)`는 모델, 추론 강도, 승인 정책, 추가 디렉터리, 네트워크 접근 및 웹 검색 모드를 구성합니다. 레거시 `web_search_enabled`보다 `web_search_mode`을 우선 사용하세요.
-   턴 기본값: `default_turn_options=TurnOptions(...)`는 `idle_timeout_seconds` 및 선택적 취소 `signal`와 같은 턴별 동작을 구성합니다.
-   도구 I/O: 도구 호출에는 `{ "type": "text", "text": ... }` 또는 `{ "type": "local_image", "path": ... }`을 포함하는 `inputs` 항목이 하나 이상 있어야 합니다. `output_schema`을 사용하면 구조화된 Codex 응답을 요구할 수 있습니다.

스레드 재사용과 지속성은 별도의 제어 항목입니다.

-   `persist_session=True`는 동일한 도구 인스턴스의 반복 호출에 하나의 Codex 스레드를 재사용합니다.
-   `use_run_context_thread_id=True`은 동일한 변경 가능 컨텍스트 객체를 공유하는 여러 실행에서 실행 컨텍스트에 스레드 ID를 저장하고 재사용합니다.
-   스레드 ID 우선순위는 호출별 `thread_id`, 실행 컨텍스트 스레드 ID(활성화된 경우), 구성된 `thread_id` 옵션 순입니다.
-   기본 실행 컨텍스트 키는 `name="codex"`의 경우 `codex_thread_id`, `name="codex_<suffix>"`의 경우 `codex_thread_id_<suffix>`입니다. `run_context_thread_id_key`로 재정의할 수 있습니다.

런타임 구성:

-   인증: `CODEX_API_KEY`(권장) 또는 `OPENAI_API_KEY`를 설정하거나 `codex_options={"api_key": "..."}`을 전달하세요.
-   런타임: `codex_options.base_url`은 CLI 기본 URL을 재정의합니다.
-   바이너리 확인: CLI 경로를 고정하려면 `codex_options.codex_path_override` 또는 `CODEX_PATH`을 설정하세요. 그렇지 않으면 SDK가 `PATH`에서 `codex`를 확인한 다음 번들로 제공되는 공급업체 바이너리를 사용합니다.
-   환경: `codex_options.env`은 하위 프로세스 환경을 완전히 제어합니다. 이 값을 제공하면 하위 프로세스가 `os.environ`을 상속하지 않습니다.
-   스트림 제한: `codex_options.codex_subprocess_stream_limit_bytes` 또는 `OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES`는 stdout/stderr 리더 제한을 제어합니다. 유효 범위는 `65536`부터 `67108864`까지이며, 기본값은 `8388608`입니다.
-   스트리밍: `on_stream`은 스레드/턴 수명 주기 이벤트와 항목 이벤트(`reasoning`, `command_execution`, `mcp_tool_call`, `file_change`, `web_search`, `todo_list`, `error` 항목 업데이트)를 수신합니다.
-   출력: 결과에는 `response`, `usage`, `thread_id`이 포함되며, 사용량은 `RunContextWrapper.usage`에 추가됩니다.

참조:

-   [Codex 도구 API 레퍼런스](../ref/extensions/experimental/codex/codex_tool.md)
-   [ThreadOptions 레퍼런스](../ref/extensions/experimental/codex/thread_options.md)
-   [TurnOptions 레퍼런스](../ref/extensions/experimental/codex/turn_options.md)
-   완전한 실행 가능 샘플은 `examples/tools/codex.py` 및 `examples/tools/codex_same_thread.py`을 참고하세요.