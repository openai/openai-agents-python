from unittest.mock import AsyncMock, Mock

import pytest

from agents import RunConfig, Runner
from examples.financial_research_agent.agents.planner_agent import (
    FinancialSearchItem,
    FinancialSearchPlan,
    planner_agent,
)
from examples.financial_research_agent.agents.search_agent import (
    FinancialSearchSummary,
    search_agent,
)
from examples.financial_research_agent.agents.verifier_agent import VerificationResult
from examples.financial_research_agent.agents.writer_agent import FinancialReportData
from examples.financial_research_agent.manager import (
    FinancialResearchManager,
    FinancialSearchEvidence,
    FinancialSource,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("planned_count", "expected_count"), [(5, 5), (15, 15), (18, 15)])
async def test_financial_research_bounds_planned_searches(
    monkeypatch: pytest.MonkeyPatch, planned_count: int, expected_count: int
) -> None:
    plan = FinancialSearchPlan(
        searches=[
            FinancialSearchItem(query=f"query {index}", reason=f"reason {index}")
            for index in range(planned_count)
        ]
    )
    planner_result = Mock(final_output=plan, final_output_as=Mock(return_value=plan))
    search_result = Mock(
        final_output_as=Mock(return_value=FinancialSearchSummary(summary="Supported summary")),
        new_items=[
            {
                "raw_item": {
                    "type": "web_search_call",
                    "action": {"sources": [{"type": "url", "url": "https://example.com/report"}]},
                }
            }
        ],
    )
    run_agent = AsyncMock(side_effect=[planner_result] + [search_result] * planned_count)
    monkeypatch.setattr(Runner, "run", run_agent)
    manager = object.__new__(FinancialResearchManager)
    manager.printer = Mock()
    manager.run_config = RunConfig()
    manager.research_cutoff = "2026-09-21"
    produce_report = AsyncMock(
        return_value=(
            FinancialReportData(
                short_summary="Summary", markdown_report="Report", follow_up_questions=[]
            ),
            VerificationResult(verified=True, issues=[]),
        )
    )
    monkeypatch.setattr(manager, "_produce_verified_report", produce_report)

    await manager.run("Analyze a company")

    assert run_agent.await_count == 1 + expected_count
    assert run_agent.await_args_list[0].args == (planner_agent, "Query: Analyze a company")
    searches = run_agent.await_args_list[1:]
    assert all(call.args[0] is search_agent for call in searches)
    assert {call.args[1] for call in searches} == {
        f"Search term: query {index}\nReason: reason {index}" for index in range(expected_count)
    }
    produce_report.assert_awaited_once()
    assert produce_report.await_args is not None
    evidence = produce_report.await_args.args[1]
    assert len(evidence) == expected_count
    assert {item.query for item in evidence} == {
        f"query {index}" for index in range(expected_count)
    }
    manager.printer.update_item.assert_any_call(
        "planning", f"Will perform {expected_count} searches", is_done=True
    )
    manager.printer.update_item.assert_any_call(
        "searching",
        f"Searches finished: {expected_count}/{expected_count} succeeded",
        is_done=True,
    )
    manager.printer.end.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_codex", [False, True])
@pytest.mark.parametrize("with_sources", [False, True])
@pytest.mark.allow_call_model_methods
async def test_financial_search_uses_responses_metadata_in_both_modes(
    monkeypatch: pytest.MonkeyPatch, use_codex: bool, with_sources: bool
) -> None:
    import json

    import httpx
    from openai import AsyncOpenAI

    from agents.models.codex import CodexProvider
    from agents.models.openai_provider import OpenAIProvider
    from examples.financial_research_agent import manager as manager_module

    requests: list[dict[str, object]] = []
    source_url = "https://example.com/report"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        action: dict[str, object] = {"type": "search", "query": "annual report"}
        if with_sources:
            action["sources"] = [{"type": "url", "url": source_url}]
        return httpx.Response(
            200,
            json={
                "id": "resp_financial",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": requests[-1]["model"],
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "ws_financial",
                        "status": "completed",
                        "action": action,
                    },
                    {
                        "id": "msg_financial",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {"summary": f"Revenue increased. Source: {source_url}"}
                                ),
                                "annotations": [],
                            }
                        ],
                    },
                ],
            },
        )

    # Mock the provider wire so the real Runner and Responses adapter process the metadata.
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = AsyncOpenAI(api_key="test-key", http_client=http_client)
        provider = OpenAIProvider(openai_client=client, use_responses=True)
        monkeypatch.setattr(manager_module, "OpenAIProvider", lambda **kwargs: provider)
        config = RunConfig(model_provider=CodexProvider()) if use_codex else RunConfig()
        manager = FinancialResearchManager(run_config=config)
        result = await manager._search(FinancialSearchItem(query="annual report", reason="metrics"))

    assert len(requests) == 1
    includes = requests[0]["include"]
    assert isinstance(includes, list) and "web_search_call.action.sources" in includes
    tools = requests[0]["tools"]
    assert isinstance(tools, list) and tools[0]["type"] == "web_search"
    if with_sources:
        assert result is not None
        assert [source.url for source in result.sources] == [source_url]
    else:
        # A URL in model-written prose alone never counts as retrieved evidence.
        assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("use_codex", [False, True])
async def test_financial_main_selects_provider_without_global_defaults(
    monkeypatch: pytest.MonkeyPatch, use_codex: bool
) -> None:
    from agents.models.codex import CodexProvider
    from examples.financial_research_agent import main as main_module

    manager = Mock(run=AsyncMock())
    constructor = Mock(return_value=manager)
    monkeypatch.setattr(main_module, "FinancialResearchManager", constructor)
    monkeypatch.setattr(main_module, "input_with_fallback", lambda *args: "Analyze revenue")

    await main_module.main(use_codex=use_codex)

    config = constructor.call_args.kwargs["run_config"]
    assert isinstance(config.model_provider, CodexProvider) is use_codex
    manager.run.assert_awaited_once_with("Analyze revenue")


@pytest.mark.asyncio
async def test_financial_run_config_reaches_orchestration_and_specialist(
    monkeypatch: pytest.MonkeyPatch, primary_audit: object
) -> None:
    from agents.testing import ScriptedModel, assistant_message, function_call

    plan = FinancialSearchPlan(
        searches=[FinancialSearchItem(query="annual report", reason="metrics")]
    )
    original = FinancialReportData(
        short_summary="Original",
        markdown_report="Original report [source](https://example.com/report)",
        follow_up_questions=[],
    )
    model = ScriptedModel(
        steps=[
            [assistant_message(plan.model_dump_json())],
            [function_call("fundamentals_analysis", {"input": "metrics"}, call_id="fundamentals")],
            [assistant_message('{"summary":"Supported financial analysis"}')],
            [assistant_message(original.model_dump_json())],
            [assistant_message('{"verified":false,"issues":[]}')],
            [assistant_message('{"verified":true,"issues":[]}')],
        ]
    )
    manager = FinancialResearchManager(run_config=RunConfig(model=model))
    manager.printer = Mock()
    monkeypatch.setattr(manager, "_audit_primary_sources", AsyncMock(return_value=primary_audit))

    assert await manager._plan_searches("Analyze revenue") == plan
    evidence = FinancialSearchEvidence(
        query="annual report",
        reason="metrics",
        summary="Supported financial analysis",
        sources=[FinancialSource(title="Annual report", url="https://example.com/report")],
        retrieved_at=manager.research_cutoff,
    )
    report, verification = await manager._produce_verified_report("Analyze revenue", [evidence])

    assert "# Primary-source findings" in report.markdown_report
    assert verification.verified
    assert len(model.calls) == 6
    model.assert_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report_text", "has_issues", "expected_verified"),
    [
        ("Revenue increased.", False, False),
        ("Source: https://example.com/report.evil", False, False),
        ("Source: https://example.com/report?invented=true", False, False),
        ("Source: https://evil.example/https://example.com/report", False, False),
        ("Revenue increased ([source](https://example.com/report)).", False, True),
        ("Revenue increased ([source](https://example.com/report)).", True, False),
    ],
)
async def test_financial_verification_enforces_source_presence_and_unresolved_issues(
    monkeypatch: pytest.MonkeyPatch,
    report_text: str,
    has_issues: bool,
    expected_verified: bool,
    primary_audit: object,
) -> None:
    from examples.financial_research_agent.agents.verifier_agent import VerificationIssue

    issues = (
        [
            VerificationIssue(
                claim="Revenue increased",
                category="unsupported",
                explanation="The evidence does not support the claim.",
                source_urls=[],
            )
        ]
        if has_issues
        else []
    )
    verdict = VerificationResult(verified=True, issues=issues)
    monkeypatch.setattr(
        Runner, "run", AsyncMock(return_value=Mock(final_output_as=Mock(return_value=verdict)))
    )
    manager = FinancialResearchManager()
    manager.printer = Mock()
    monkeypatch.setattr(manager, "_audit_primary_sources", AsyncMock(return_value=primary_audit))
    evidence = FinancialSearchEvidence(
        query="annual report",
        reason="metrics",
        summary="Supported financial analysis",
        sources=[FinancialSource(title="Annual report", url="https://example.com/report")],
        retrieved_at=manager.research_cutoff,
    )
    report = FinancialReportData(
        short_summary="Summary", markdown_report=report_text, follow_up_questions=[]
    )

    result = await manager._verify_report("Analyze revenue", report, [evidence])

    assert result.verified is expected_verified
    if not expected_verified:
        assert result.issues


@pytest.fixture
def primary_audit() -> object:
    from examples.financial_research_agent.agents.primary_source_auditor import (
        PrimarySourceAudit,
        PrimarySourceCheck,
    )

    audit = PrimarySourceAudit(
        checks=[
            PrimarySourceCheck(
                claim="Revenue increased",
                source_url="https://example.com/report",
                supporting_extract="Revenue increased",
                supported=True,
                explanation="The primary document supports the report.",
            ),
            PrimarySourceCheck(
                claim="Revenue was $123 billion.",
                source_url="https://example.com/report",
                supporting_extract="Revenue was $123 billion.",
                supported=True,
                explanation="The primary document supports this metric.",
            ),
        ]
    )
    audit.findings = [
        check.model_copy(update={"dimension": dimension})
        for check, dimension in zip(audit.checks, ["Revenue drivers", "Risks"], strict=True)
    ]
    return audit, {"https://example.com/report"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supported", "status", "action_type"),
    [
        (False, "completed", "open_page"),
        (True, "failed", "open_page"),
        (True, "completed", "search"),
        (True, "completed", "open_page"),
    ],
)
async def test_primary_audit_blocks_false_summary_approval_and_requires_completed_page_reads(
    monkeypatch: pytest.MonkeyPatch, supported: bool, status: str, action_type: str
) -> None:
    from examples.financial_research_agent.agents.primary_source_auditor import (
        AuditedVerificationResult,
        PrimarySourceAudit,
        PrimarySourceCheck,
    )

    url = "https://issuer.example/annual-financial-statements.pdf"
    claim = f"FY2026 Services revenue was ${'123.301' if supported else '105'} billion."
    audit = PrimarySourceAudit(
        checks=[
            PrimarySourceCheck(
                claim=claim,
                source_url=url,
                supporting_extract="Services 123,301 (in millions)",
                supported=supported,
                explanation=(
                    "The primary statement reports FY2026 Services revenue of $123.301 billion, "
                    "not $105 billion."
                ),
            )
        ]
    )
    audit_result = Mock(
        final_output_as=Mock(return_value=audit),
        new_items=[
            {
                "raw_item": {
                    "type": "web_search_call",
                    "status": status,
                    "action": {"type": action_type, "url": url},
                }
            }
        ],
    )
    accepted = Mock(final_output_as=Mock(return_value=VerificationResult(verified=True, issues=[])))
    run_agent = AsyncMock(side_effect=[audit_result, accepted])
    monkeypatch.setattr(Runner, "run", run_agent)
    manager = FinancialResearchManager()
    manager.printer = Mock()
    report = FinancialReportData(
        short_summary=claim,
        markdown_report=f"{claim} [Source]({url})",
        follow_up_questions=[],
    )
    evidence = FinancialSearchEvidence(
        query="annual services revenue",
        reason="metrics",
        summary="FY2026 Services revenue was $105 billion.",
        sources=[FinancialSource(title="Annual statements", url=url)],
        retrieved_at=manager.research_cutoff,
    )

    monkeypatch.setattr(
        manager,
        "_read_primary_source_claims",
        AsyncMock(return_value=(PrimarySourceAudit(checks=[]), set())),
    )
    result = await manager._verify_report("Analyze issuer", report, [evidence])

    assert isinstance(result, AuditedVerificationResult)
    assert result.verified is (supported and status == "completed" and action_type == "open_page")
    assert result.opened_source_urls == (
        [url] if status == "completed" and action_type == "open_page" else []
    )
    assert result.primary_source_checks == audit.checks
    if not result.verified:
        assert "123.301" in result.issues[0].explanation
    import json

    verifier_payload = json.loads(run_agent.await_args_list[1].args[1])
    assert verifier_payload["primary_source_audit"] == audit.model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_claim", [False, True])
async def test_primary_source_recovery_requires_a_fresh_matching_check_and_read(
    monkeypatch: pytest.MonkeyPatch, matching_claim: bool
) -> None:
    from examples.financial_research_agent.agents.primary_source_auditor import (
        PrimarySourceAudit,
        PrimarySourceCheck,
    )

    url = "https://issuer.example/statements.pdf"
    claim = "Services revenue was $123.301 billion in fiscal 2026."
    original = PrimarySourceCheck(
        dimension="Risks",
        claim=claim,
        source_url=url,
        supporting_extract="An unproven initial extract",
        supported=True,
        explanation="The initial audit claimed support without opening this document.",
    )
    fresh = original.model_copy(
        update={
            "claim": claim if matching_claim else "A different claim",
            "supporting_extract": (
                "Services 123,301; twelve months ended September 26, 2026; in millions"
            ),
            "explanation": (
                "The newly opened primary document establishes the exact metric and period."
            ),
        }
    )
    initial_result = Mock(
        final_output_as=Mock(return_value=PrimarySourceAudit(checks=[original])), new_items=[]
    )
    monkeypatch.setattr(Runner, "run", AsyncMock(return_value=initial_result))
    manager = FinancialResearchManager()
    read_claims = AsyncMock(return_value=(PrimarySourceAudit(checks=[fresh]), {url}))
    monkeypatch.setattr(manager, "_read_primary_source_claims", read_claims)
    report = FinancialReportData(short_summary=claim, markdown_report=claim, follow_up_questions=[])

    audit, opened = await manager._audit_primary_sources("Analyze issuer", report)

    read_claims.assert_awaited_once_with(url, [claim])
    assert opened == {url}
    assert audit.checks[0].supported is matching_claim
    assert audit.checks[0].dimension == "Risks"
    if matching_claim:
        assert audit.checks[0].supporting_extract == fresh.supporting_extract
    else:
        assert "did not recheck" in audit.checks[0].explanation


@pytest.mark.asyncio
async def test_financial_audited_revision_uses_only_confirmed_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.financial_research_agent.agents.primary_source_auditor import (
        AuditedVerificationResult,
        PrimarySourceCheck,
    )

    url = "https://issuer.example/statements.pdf"
    confirmed = [
        PrimarySourceCheck(
            dimension="Revenue drivers",
            claim="Revenue was approximately $109.2 billion.",
            source_url=url,
            supporting_extract="Services: 109,158 (in millions), fiscal 2025.",
            supported=True,
            explanation="Confirmed against the opened primary statement.",
        ),
        PrimarySourceCheck(
            dimension="Risks",
            claim="Profit may fall because of competition.",
            source_url=url,
            supporting_extract="Our markets are subject to aggressive price competition.",
            supported=True,
            explanation="Confirmed against the opened primary statement.",
        ),
    ]
    unsupported = confirmed[0].model_copy(
        update={"claim": "Revenue will double.", "supported": False}
    )
    unread = confirmed[0].model_copy(
        update={"claim": "Profit was $80 billion.", "source_url": "https://unread.example/report"}
    )
    verdict = AuditedVerificationResult(
        verified=False,
        issues=[],
        primary_source_checks=[*confirmed, unsupported, unread],
        primary_source_findings=[*confirmed, unsupported, unread],
        opened_source_urls=[url],
    )
    manager = FinancialResearchManager()
    manager.printer = Mock()
    run_agent = AsyncMock()
    monkeypatch.setattr(Runner, "run", run_agent)
    original = FinancialReportData(
        short_summary="Revenue will double.",
        markdown_report="Unsupported draft",
        follow_up_questions=[],
    )

    revised = await manager._revise_report("Analyze issuer", original, [], verdict)

    for check in confirmed:
        assert check.supporting_extract in revised.markdown_report
    assert "Revenue will double" not in revised.markdown_report
    assert "Profit was" not in revised.markdown_report
    assert "# Primary-source findings" in revised.markdown_report
    assert "### Revenue drivers" in revised.markdown_report
    assert "### Risks" in revised.markdown_report
    assert "$109.2" not in revised.markdown_report
    assert "because of" not in revised.markdown_report
    run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_revision_combines_previously_read_revenue_and_new_risk_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.financial_research_agent.agents.primary_source_auditor import (
        AuditedVerificationResult,
        PrimarySourceCheck,
    )

    url = "https://issuer.example/annual-report"

    def finding(dimension: str, excerpt: str) -> PrimarySourceCheck:
        return PrimarySourceCheck(
            dimension=dimension,
            claim=excerpt,
            source_url=url,
            supporting_extract=excerpt,
            supported=True,
            analysis="This observation may matter for the requested dimension.",
            explanation="Confirmed against the opened primary report.",
        )

    revenue = [
        finding("Revenue drivers", "Services sales increased."),
        finding("Revenue drivers", "Device sales increased."),
    ]
    risks = [
        finding("Key risks", "Our markets face aggressive competition."),
        finding("Key risks", "We depend on carriers and other resellers."),
    ]

    def rejected(findings: list[PrimarySourceCheck]) -> AuditedVerificationResult:
        return AuditedVerificationResult(
            verified=False,
            issues=[],
            primary_source_checks=[],
            primary_source_findings=findings,
            opened_source_urls=[url],
        )

    manager = FinancialResearchManager()
    manager.printer = Mock()
    original = FinancialReportData(
        short_summary="Draft", markdown_report="Draft", follow_up_questions=[]
    )
    monkeypatch.setattr(manager, "_write_report", AsyncMock(return_value=original))
    verify = AsyncMock(
        side_effect=[
            rejected(revenue),
            rejected(risks),
            VerificationResult(verified=True, issues=[]),
        ]
    )
    monkeypatch.setattr(manager, "_verify_report", verify)

    report, verification = await manager._produce_verified_report("Analyze drivers and risks", [])

    assert verification.verified
    assert "### Revenue drivers" in report.markdown_report
    assert "### Key risks" in report.markdown_report
    assert all(
        finding.supporting_extract in report.markdown_report for finding in [*revenue, *risks]
    )
    assert verify.await_count == 3
