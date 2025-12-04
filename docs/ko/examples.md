---
search:
  exclude: true
---
# 코드 예제

[레포지토리](https://github.com/openai/openai-agents-python/tree/main/examples)의 examples 섹션에서 SDK의 다양한 샘플 구현을 확인하세요. 예제는 다양한 패턴과 기능을 보여주는 여러 카테고리로 구성되어 있습니다.

## 카테고리

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    이 카테고리의 예제는 다음과 같은 일반적인 에이전트 설계 패턴을 보여줍니다

    -   결정적 워크플로
    -   도구로서의 에이전트
    -   에이전트 병렬 실행
    -   조건부 도구 사용
    -   입력/출력 가드레일
    -   판정자로서의 LLM
    -   라우팅
    -   스트리밍 가드레일

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    이 예제들은 SDK의 기초 기능을 보여줍니다

    -   Hello World 예제 (기본 모델, GPT-5, 오픈 웨이트 모델)
    -   에이전트 라이프사이클 관리
    -   동적 system prompts
    -   스트리밍 출력 (텍스트, 아이템, 함수 호출 인자)
    -   프롬프트 템플릿
    -   파일 처리 (로컬 및 원격, 이미지와 PDF)
    -   사용량 추적
    -   비엄격 출력 타입
    -   이전 응답 ID 사용

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    항공사용 고객 서비스 시스템 예제

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    금융 데이터 분석을 위한 에이전트와 도구로 구조화된 연구 워크플로를 보여주는 금융 리서치 에이전트

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    메시지 필터링과 함께 에이전트 핸드오프의 실제 예제

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    호스티드 MCP (Model Context Protocol) 커넥터와 승인 사용 방법을 보여주는 예제

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol)로 에이전트를 구축하는 방법을 학습하세요. 다음을 포함합니다

    -   파일시스템 예제
    -   Git 예제
    -   MCP prompt 서버 예제
    -   SSE (Server-Sent Events) 예제
    -   스트리밍 가능한 HTTP 예제

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    에이전트를 위한 다양한 메모리 구현 예제

    -   SQLite 세션 스토리지
    -   고급 SQLite 세션 스토리지
    -   Redis 세션 스토리지
    -   SQLAlchemy 세션 스토리지
    -   암호화된 세션 스토리지
    -   OpenAI 세션 스토리지

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    커스텀 프로바이더와 LiteLLM 통합을 포함해, OpenAI 이외의 모델을 SDK와 함께 사용하는 방법을 알아보세요

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK를 사용해 실시간 경험을 구축하는 방법을 보여주는 예제

    -   웹 애플리케이션
    -   명령줄 인터페이스
    -   Twilio 연동

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    추론 콘텐츠 및 structured outputs로 작업하는 방법을 보여주는 예제

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    복잡한 멀티 에이전트 연구 워크플로를 보여주는 간단한 딥 리서치 클론

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    다음과 같은 OpenAI 호스트하는 도구를 구현하는 방법을 학습하세요

    -   웹 검색 및 필터가 있는 웹 검색
    -   파일 검색
    -   Code interpreter
    -   컴퓨터 사용
    -   이미지 생성

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS 및 STT 모델을 사용하는 음성 에이전트 예제를 확인하세요. 스트리밍 음성 예제 포함