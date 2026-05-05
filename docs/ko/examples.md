---
search:
  exclude: true
---
# 코드 예제

[repo](https://github.com/openai/openai-agents-python/tree/main/examples)의 examples 섹션에서 SDK의 다양한 샘플 구현을 확인해 보세요. examples는 서로 다른 패턴과 기능을 보여주는 여러 카테고리로 구성되어 있습니다.

## 카테고리

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    이 카테고리의 예제는 다음과 같은 일반적인 에이전트 설계 패턴을 보여줍니다

    -   결정론적 워크플로
    -   Agents as tools
    -   스트리밍 이벤트를 포함한 Agents as tools (`examples/agent_patterns/agents_as_tools_streaming.py`)
    -   구조화된 입력 매개변수를 포함한 Agents as tools (`examples/agent_patterns/agents_as_tools_structured.py`)
    -   병렬 에이전트 실행
    -   조건부 도구 사용
    -   서로 다른 동작으로 도구 사용 강제 (`examples/agent_patterns/forcing_tool_use.py`)
    -   입출력 가드레일
    -   심판 역할의 LLM
    -   라우팅
    -   스트리밍 가드레일
    -   도구 승인 및 상태 직렬화를 포함한 휴먼인더루프 (HITL) (`examples/agent_patterns/human_in_the_loop.py`)
    -   스트리밍을 포함한 휴먼인더루프 (HITL) (`examples/agent_patterns/human_in_the_loop_stream.py`)
    -   승인 플로를 위한 사용자 지정 거절 메시지 (`examples/agent_patterns/human_in_the_loop_custom_rejection.py`)

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    이 예제들은 다음과 같은 SDK의 기본 기능을 보여줍니다

    -   Hello World 예제 (기본 모델, GPT-5, 오픈 웨이트 모델)
    -   에이전트 라이프사이클 관리
    -   실행 훅 및 에이전트 훅 라이프사이클 예제 (`examples/basic/lifecycle_example.py`)
    -   동적 시스템 프롬프트
    -   기본 도구 사용 (`examples/basic/tools.py`)
    -   도구 입출력 가드레일 (`examples/basic/tool_guardrails.py`)
    -   이미지 도구 출력 (`examples/basic/image_tool_output.py`)
    -   스트리밍 출력 (텍스트, 항목, 함수 호출 인자)
    -   턴 간 공유 세션 헬퍼를 사용하는 Responses websocket 전송 (`examples/basic/stream_ws.py`)
    -   프롬프트 템플릿
    -   파일 처리 (로컬 및 원격, 이미지 및 PDF)
    -   사용량 추적
    -   Runner 관리 재시도 설정 (`examples/basic/retry.py`)
    -   서드파티 어댑터를 통한 Runner 관리 재시도 (`examples/basic/retry_litellm.py`)
    -   비엄격 출력 타입
    -   이전 응답 ID 사용

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    항공사를 위한 고객 서비스 시스템 예제입니다

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    금융 데이터 분석을 위한 에이전트와 도구를 사용한 구조화된 리서치 워크플로를 보여주는 금융 리서치 에이전트입니다

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    메시지 필터링을 포함한 에이전트 핸드오프의 실용적인 예제:

    -   메시지 필터 예제 (`examples/handoffs/message_filter.py`)
    -   스트리밍을 포함한 메시지 필터 (`examples/handoffs/message_filter_streaming.py`)

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    OpenAI Responses API와 함께 호스티드 MCP (Model context protocol)를 사용하는 방법을 보여주는 예제:

    -   승인 없는 간단한 호스티드 MCP (`examples/hosted_mcp/simple.py`)
    -   Google Calendar 같은 MCP 커넥터 (`examples/hosted_mcp/connectors.py`)
    -   인터럽션(중단 처리) 기반 승인을 포함한 휴먼인더루프 (HITL) (`examples/hosted_mcp/human_in_the_loop.py`)
    -   MCP 도구 호출용 승인 시 콜백 (`examples/hosted_mcp/on_approval.py`)

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model context protocol)로 에이전트를 구축하는 방법을 알아보세요:

    -   파일시스템 예제
    -   Git 예제
    -   MCP 프롬프트 서버 예제
    -   SSE (Server-Sent Events) 예제
    -   SSE 원격 서버 연결 (`examples/mcp/sse_remote_example`)
    -   Streamable HTTP 예제
    -   Streamable HTTP 원격 연결 (`examples/mcp/streamable_http_remote_example`)
    -   Streamable HTTP용 사용자 지정 HTTP 클라이언트 팩토리 (`examples/mcp/streamablehttp_custom_client_example`)
    -   `MCPUtil.get_all_function_tools`를 사용한 모든 MCP 도구 프리패칭 (`examples/mcp/get_all_mcp_tools_example`)
    -   FastAPI를 사용하는 MCPServerManager (`examples/mcp/manager_example`)
    -   MCP 도구 필터링 (`examples/mcp/tool_filter_example`)

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    에이전트를 위한 다양한 메모리 구현 예제:

    -   SQLite 세션 저장소
    -   고급 SQLite 세션 저장소
    -   Redis 세션 저장소
    -   SQLAlchemy 세션 저장소
    -   Dapr 상태 저장소 세션 저장소
    -   암호화된 세션 저장소
    -   OpenAI Conversations 세션 저장소
    -   Responses 컴팩션 세션 저장소
    -   `ModelSettings(store=False)`를 사용한 상태 비저장 Responses 컴팩션 (`examples/memory/compaction_session_stateless_example.py`)
    -   파일 기반 세션 저장소 (`examples/memory/file_session.py`)
    -   휴먼인더루프 (HITL)를 포함한 파일 기반 세션 (`examples/memory/file_hitl_example.py`)
    -   휴먼인더루프 (HITL)를 포함한 SQLite 인메모리 세션 (`examples/memory/memory_session_hitl_example.py`)
    -   휴먼인더루프 (HITL)를 포함한 OpenAI Conversations 세션 (`examples/memory/openai_session_hitl_example.py`)
    -   세션 전반의 HITL 승인/거절 시나리오 (`examples/memory/hitl_session_scenario.py`)

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    사용자 지정 프로바이더와 서드파티 어댑터를 포함해 SDK에서 OpenAI 이외 모델을 사용하는 방법을 살펴보세요

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK를 사용해 실시간 경험을 구축하는 방법을 보여주는 예제:

    -   구조화된 텍스트 및 이미지 메시지를 사용하는 웹 애플리케이션 패턴
    -   커맨드라인 오디오 루프 및 재생 처리
    -   WebSocket을 통한 Twilio Media Streams 통합
    -   Realtime Calls API attach 플로를 사용하는 Twilio SIP 통합

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content를 다루는 방법을 보여주는 예제:

    -   Runner API의 reasoning content, 스트리밍 및 비스트리밍 (`examples/reasoning_content/runner_example.py`)
    -   OpenRouter를 통한 OSS 모델의 reasoning content (`examples/reasoning_content/gpt_oss_stream.py`)
    -   기본 reasoning content 예제 (`examples/reasoning_content/main.py`)

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    복잡한 멀티 에이전트 리서치 워크플로를 보여주는 간단한 딥 리서치 클론입니다

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    다음과 같은 OpenAI 호스트하는 도구 및 실험적 Codex 도구 기능을 구현하는 방법을 알아보세요:

    -   웹 검색 및 필터를 포함한 웹 검색
    -   파일 검색
    -   Code Interpreter
    -   파일 편집 및 승인을 포함한 패치 적용 도구 (`examples/tools/apply_patch.py`)
    -   승인 콜백을 포함한 셸 도구 실행 (`examples/tools/shell.py`)
    -   휴먼인더루프 (HITL) 인터럽션(중단 처리) 기반 승인을 포함한 셸 도구 (`examples/tools/shell_human_in_the_loop.py`)
    -   인라인 스킬을 포함한 호스티드 컨테이너 셸 (`examples/tools/container_shell_inline_skill.py`)
    -   스킬 참조를 포함한 호스티드 컨테이너 셸 (`examples/tools/container_shell_skill_reference.py`)
    -   로컬 스킬을 포함한 로컬 셸 (`examples/tools/local_shell_skill.py`)
    -   네임스페이스 및 지연 도구를 사용하는 도구 검색 (`examples/tools/tool_search.py`)
    -   컴퓨터 사용
    -   이미지 생성
    -   실험적 Codex 도구 워크플로 (`examples/tools/codex.py`)
    -   실험적 Codex 동일 스레드 워크플로 (`examples/tools/codex_same_thread.py`)

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    스트리밍 음성 예제를 포함해 TTS 및 STT 모델을 사용하는 음성 에이전트 예제를 확인해 보세요