"""Verify shared services preserve independent Session state and credentials."""

from __future__ import annotations

import json

import pytest

from agents import Agent, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, responses as responses, text_response


async def test_other_session_survives_compaction_and_scope_close(
    responses: ResponsesFixture,
) -> None:
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-entry-key")
    agent = Agent(instructions="Answer briefly.", model="gpt-5.2", provider=provider)
    responses.respond(*text_response("a-answer", "a-output"))
    responses.respond(*text_response("b-answer", "b-output"))
    responses.respond(*text_response("b-followup", "b-followup-output"))
    async with timeout(30), provider:
        async with agent.session() as b:
            b_id = b.id
            async with agent.session() as a:
                assert a.id != b.id
                a_id = a.id
                assert (
                    await (await a.submit("private-a-input")).get_result()
                ).final_output == "a-output"
                assert (
                    await (await b.submit("private-b-input")).get_result()
                ).final_output == "b-output"
                # This requires an exclusively owned root registry. A shared
                # whole ThreadManager would fail while the sibling stays open.
                await a.compact(summary="private-a-summary")
                assert a.id == a_id
            assert b.id == b_id
            assert (
                await (await b.submit("b-again")).get_result()
            ).final_output == "b-followup-output"
        assert len(responses.requests) == 3
        b_input = json.dumps(responses.requests[-1]["input"])
        assert "private-b-input" in b_input
        assert "b-output" in b_input
        assert "b-again" in b_input
        assert "private-a-input" not in b_input
        assert "a-output" not in b_input
        assert "private-a-summary" not in b_input


async def test_distinct_provider_credentials_stay_distinct(
    responses: ResponsesFixture,
) -> None:
    first = NativeProvider(base_url=responses.url, api_key="synthetic-first-key")
    second = NativeProvider(base_url=responses.url, api_key="synthetic-second-key")
    responses.respond(*text_response("first-answer", "first"))
    responses.respond(*text_response("second-answer", "second"))
    async with timeout(30), first, second:
        for provider, expected in [(first, "first"), (second, "second")]:
            result = await Agent(instructions="Answer briefly.", provider=provider).run(expected)
            assert result.final_output == expected
    assert [header["authorization"] for header in responses.request_headers] == [
        "Bearer synthetic-first-key",
        "Bearer synthetic-second-key",
    ]


async def test_environment_key_rotation_keeps_live_auth_and_fresh_histories(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_key = "synthetic-entry-environment-A"
    second_key = "synthetic-entry-environment-B"
    monkeypatch.setenv("OPENAI_API_KEY", first_key)
    provider = NativeProvider(base_url=responses.url, api_key=None, supports_websockets=False)
    agent = Agent(instructions="Answer briefly.", model="gpt-5.2", provider=provider)
    for index, output in enumerate(("first-A", "fresh-B", "live-A", "restored-A")):
        responses.respond(*text_response(f"environment-output-{index}", output))
    async with timeout(30), provider:
        async with agent.session() as original:
            first = await (await original.submit("private-A-first")).get_result()
            assert first.final_output == "first-A"
            monkeypatch.setenv("OPENAI_API_KEY", second_key)
            assert (await agent.run("private-B-only")).final_output == "fresh-B"
            # An already open Session retains its accepted A principal while
            # a fresh Session captures B on the same Provider descriptor.
            followup = await (await original.submit("private-A-followup")).get_result()
            assert followup.final_output == "live-A"
            saved = (await original.save_state()).to_json()
            assert first_key not in saved and second_key not in saved
        monkeypatch.setenv("OPENAI_API_KEY", first_key)
        assert (await agent.run("private-restored-A")).final_output == "restored-A"
    assert [header["authorization"] for header in responses.request_headers] == [
        f"Bearer {first_key}",
        f"Bearer {second_key}",
        f"Bearer {first_key}",
        f"Bearer {first_key}",
    ]
    first, second, live, restored = [json.dumps(row["input"]) for row in responses.requests]
    assert "private-A-first" in first and "private-B-only" not in first
    assert "private-B-only" in second and "private-A-first" not in second
    assert "private-A-first" in live and "private-A-followup" in live
    assert "private-B-only" not in live
    assert "private-restored-A" in restored
    assert all(
        marker not in restored
        for marker in ("private-A-first", "private-A-followup", "private-B-only")
    )


@pytest.mark.parametrize("missing_key", [None, "   "], ids=["missing", "blank"])
async def test_missing_environment_key_cannot_reuse_a_warm_provider_principal(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, missing_key: str | None
) -> None:
    key = "synthetic-entry-environment-present"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-entry-ignored-legacy")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "synthetic-entry-ignored-token")
    provider = NativeProvider(base_url=responses.url, api_key=None, supports_websockets=False)
    agent = Agent(instructions="Answer briefly.", model="gpt-5.2", provider=provider)
    async with timeout(30), provider:
        responses.respond(*text_response("before-missing-key", "before"))
        assert (await agent.run("before-missing-key")).final_output == "before"
        if missing_key is None:
            monkeypatch.delenv("OPENAI_API_KEY")
        else:
            monkeypatch.setenv("OPENAI_API_KEY", missing_key)
        with pytest.raises(RuntimeError, match="^Provide api_key or set OPENAI_API_KEY$"):
            async with agent.session():
                pytest.fail("A missing captured API key must fail before Session publication")
        assert len(responses.requests) == 1
        monkeypatch.setenv("OPENAI_API_KEY", key)
        responses.respond(*text_response("after-restored-key", "after"))
        assert (await agent.run("after-restored-key")).final_output == "after"
    assert [header["authorization"] for header in responses.request_headers] == [
        f"Bearer {key}",
        f"Bearer {key}",
    ]
    assert "before-missing-key" not in json.dumps(responses.requests[-1]["input"])
