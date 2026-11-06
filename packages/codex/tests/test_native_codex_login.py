"""Saved Codex login behavior through the native HTTP and storage paths."""

from __future__ import annotations

import base64
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    CodexAccount,
    CodexLogin,
    Environment,
    LocalStorage,
    NativeError,
    NativeProvider,
    PromptedOutput,
    Turn,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_deferred_jobs import Journal, Provider, declared, done
from tests.test_deferred_opening_outcome import pending

ACCOUNT = "synthetic-workspace"
USER = "synthetic-user"
INSTRUCTIONS = "Follow the coding request."


def saved_login(
    home: Path,
    *,
    user: str = USER,
    account: str | None = ACCOUNT,
    token: str = "saved-token",
    mode: str = "chatgpt",
    id_account: str | None = None,
    fedramp: bool = False,
) -> Path:
    auth_claims = {
        "chatgpt_account_is_fedramp": fedramp,
        "chatgpt_user_id": user,
        "chatgpt_plan_type": "pro",
    }
    if (workspace := id_account or account) is not None:
        auth_claims["chatgpt_account_id"] = workspace
    payload = {"https://api.openai.com/auth": auth_claims}
    claims = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": mode,
                "OPENAI_API_KEY": "unused-stored-key" if mode == "apikey" else None,
                "tokens": {
                    "id_token": f"e30.{claims}.synthetic",
                    "access_token": token,
                    "refresh_token": "synthetic-refresh-token",
                    "account_id": account,
                },
                "last_refresh": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return home


@pytest.fixture(autouse=True)
def uncompressed_model_requests(monkeypatch):
    # The shared aiohttp peer can read JSON without an optional Zstandard decoder.
    def without_compression(native_create):
        def create(config, *args, **kwargs):
            config = json.loads(config)
            config["features.enable_request_compression"] = False
            return native_create(json.dumps(config), *args, **kwargs)

        return create

    native = importlib.import_module("agents._native")
    for name in ("create_engine", "create_checkpoint_engine"):
        monkeypatch.setattr(native, name, without_compression(getattr(native, name)))


@pytest.fixture
def codex_endpoint(responses, monkeypatch):
    session_module = importlib.import_module("_agents_codex.session")
    assert session_module._CODEX_RESPONSES_BASE_URL == "https://chatgpt.com/backend-api/codex"
    monkeypatch.setattr(session_module, "_CODEX_RESPONSES_BASE_URL", responses.url)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    return responses


def codex_agent(home: Path | None, **kwargs):
    return Agent(
        instructions=INSTRUCTIONS,
        model="gpt-5.1-codex",
        provider=NativeProvider(auth=CodexLogin(codex_home=home)),
        **kwargs,
    )


async def submit(session, prompt: str):
    return await (await session.submit(prompt)).get_result()


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@pytest.mark.parametrize("overlap", ["cwd", "additional", "symlink_target", "hard_link"])
async def test_local_executor_rejects_saved_login_overlap(
    codex_endpoint: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overlap: str,
) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    home = saved_login((workspace if overlap == "cwd" else tmp_path) / "login")
    auth_file = home / "auth.json"
    agent = codex_agent(home)
    roots: list[Path] = []
    reason = "writable roots must stay outside the saved Codex login"
    if overlap == "additional":
        monkeypatch.setenv("CODEX_HOME", str(home))
        agent = codex_agent(None)
        roots = [home]
    elif overlap == "symlink_target":
        target = workspace / "credentials"
        auth_file.rename(target)
        auth_file.symlink_to(target)
    elif overlap == "hard_link":
        (workspace / "ordinary-name").hardlink_to(auth_file)
        reason = "writable roots contain a hard link"

    with pytest.raises(RuntimeError, match=reason):
        async with agent.session(
            environment=Environment.local(
                cwd=workspace, writable_roots=roots, tools=["exec_command"]
            ),
        ):
            pytest.fail("A local executor opened with access to the saved login")
    assert codex_endpoint.requests == []


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires native Unix")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_local_executor_rejects_saved_login_replacement(
    codex_endpoint: ResponsesFixture, tmp_path: Path
) -> None:
    home = saved_login(tmp_path / "login")
    workspace = home / "worktree"
    workspace.mkdir()
    additional = tmp_path / "additional"
    additional.mkdir()
    alias = additional / "ordinary-name"
    alias.hardlink_to(home / "auth.json")
    agent = codex_agent(home)

    async with timeout(30), agent.session(environment=Environment.local(cwd=workspace)) as session:
        for root, reason in (
            (home, "writable roots must stay outside the saved Codex login"),
            (additional, "writable roots contain a hard link"),
        ):
            turn = await session.submit("Reject this grant.", writable_roots=[root.resolve()])
            with pytest.raises(NativeError, match=reason) as refused:
                await turn.get_result()
            assert refused.value.effects == "not_started"
        assert codex_endpoint.requests == []

        alias.unlink()
        codex_endpoint.respond(assistant_output("allowed"))
        turn = await session.submit("Allow this grant.", writable_roots=[additional.resolve()])
        assert (await turn.get_result()).final_output == "allowed"


@pytest.mark.parametrize(
    "conflict",
    [
        {"api_key": "synthetic-key"},
        {"base_url": "https://example.invalid/v1"},
        {"supports_websockets": True},
    ],
)
def test_codex_login_rejects_conflicting_provider_options(conflict):
    with pytest.raises(ValueError, match="CodexLogin requires HTTP"):
        NativeProvider(auth=CodexLogin(), **conflict)


async def test_saved_login_runs_a_coding_turn_without_api_keys(
    codex_endpoint, monkeypatch, tmp_path
):
    home = saved_login(tmp_path / "codex")
    monkeypatch.setenv("CODEX_HOME", str(home))
    peer = codex_endpoint

    @function_tool
    def read_source() -> str:
        """Read a synthetic Python module."""
        return "def greeting():\n    return 'Hello'\n"

    peer.respond(function_call("read_source", {}, "read-code"))
    peer.respond(assistant_output("The greeting function returns Hello."))
    agent = Agent(
        instructions=INSTRUCTIONS,
        model="gpt-5.1-codex",
        tools=[read_source],
        provider=NativeProvider(auth=CodexLogin(account_id=ACCOUNT)),
    )
    async with timeout(30), agent.session() as session:
        assert session.codex_account == CodexAccount(account_id=ACCOUNT, plan_type="pro")
        assert peer.requests == []
        result = await submit(session, "Read the module and summarize the Python function.")
        assert result.final_output == "The greeting function returns Hello."
        state = await session.save_state()
    assert len(peer.requests) == 2
    assert tool_output(peer.requests[1], "read-code") == "def greeting():\n    return 'Hello'\n"
    for headers in peer.request_headers:
        assert headers["authorization"] == "Bearer saved-token"
        assert headers["chatgpt-account-id"] == ACCOUNT
    state_json = state.to_json()
    assert state.codex_login_binding is not None
    for private_value in (str(home), "saved-token", "synthetic-refresh-token", ACCOUNT, USER):
        assert private_value not in state_json


@pytest.mark.parametrize("mode", [None, "apikey", "chatgptAuthTokens"])
async def test_managed_mode_refuses_missing_or_non_oauth_login_despite_env_keys(
    codex_endpoint, monkeypatch, tmp_path, mode
):
    monkeypatch.setenv("OPENAI_API_KEY", "unused-env-key")
    monkeypatch.setenv("CODEX_API_KEY", "unused-codex-key")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "unused-env-token")
    home = tmp_path / "codex"
    home.mkdir()
    if mode is not None:
        saved_login(home, mode=mode)
    with pytest.raises(RuntimeError, match="No usable saved Codex OAuth login"):
        async with codex_agent(home).session():
            pytest.fail("Opened without a saved Codex-managed OAuth login")
    assert codex_endpoint.requests == []


@pytest.mark.parametrize("account", [ACCOUNT, None], ids=["workspace", "personal"])
async def test_expected_account_checked_before_model_work(codex_endpoint, tmp_path, account):
    home = saved_login(tmp_path / "codex", account=account)
    provider = NativeProvider(auth=CodexLogin(codex_home=home, account_id="other-workspace"))
    with pytest.raises(RuntimeError, match="does not match the expected account"):
        async with Agent(instructions=INSTRUCTIONS, provider=provider).session():
            pytest.fail("Opened with the wrong account")
    assert codex_endpoint.requests == []


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("account", [ACCOUNT, None], ids=["workspace", "personal"])
async def test_resume_keeps_the_original_user_and_workspace(
    codex_endpoint, tmp_path, local, account
):
    peer = codex_endpoint
    home = saved_login(tmp_path / "codex", account=account)
    agent = codex_agent(home)
    opening = {"storage": LocalStorage(tmp_path / "threads")} if local else {}
    peer.respond(assistant_output("Original answer."))
    async with timeout(30), agent.session(**opening) as session:
        assert session.codex_account == CodexAccount(account_id=account, plan_type="pro")
        assert (await submit(session, "Start.")).final_output == "Original answer."
        restore = (
            {"storage": opening["storage"], "resume": session.id}
            if local
            else {"state": await session.save_state()}
        )

    api_agent = Agent(
        instructions=INSTRUCTIONS,
        provider=NativeProvider(base_url=peer.url, api_key="unwanted-api-key"),
    )
    with pytest.raises((RuntimeError, ValueError), match="same authentication mode"):
        async with api_agent.session(**restore):
            pytest.fail("Restored plan-backed history with an API key")

    for user, changed_account in (("other-user", account), (USER, "other-workspace")):
        saved_login(home, user=user, account=changed_account, token="wrong-token")
        with pytest.raises(RuntimeError, match="Codex login"):
            async with agent.session(**restore):
                pytest.fail("Restored with a different login")
    assert len(peer.requests) == 1

    saved_login(home, account=account, token="rotated-token")
    peer.respond(assistant_output("Continued answer."))
    async with timeout(30), agent.session(**restore) as resumed:
        assert (await submit(resumed, "Continue.")).final_output == "Continued answer."
    assert peer.request_headers[1]["authorization"] == "Bearer rotated-token"
    assert all(headers.get("chatgpt-account-id") == account for headers in peer.request_headers)
    assert "Original answer." in json.dumps(peer.requests[1]["input"])


async def test_terminal_saved_login_plan_limit_is_not_retried(codex_endpoint, tmp_path):
    home = saved_login(tmp_path / "codex")
    codex_endpoint.respond(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "type": "usage_limit_reached",
                    "message": "Synthetic plan limit.",
                }
            },
        }
    )
    codex_endpoint.respond(assistant_output("An unexpected retry succeeded."))
    async with timeout(30), codex_agent(home).session() as session:
        with pytest.raises(NativeError) as caught:
            await submit(session, "Hello.")
    assert caught.value.category == "usage_limit_exceeded"
    assert len(codex_endpoint.requests) == 1


async def test_api_key_mode_stays_on_its_explicit_provider(codex_endpoint, monkeypatch, tmp_path):
    home = saved_login(tmp_path / "codex")
    monkeypatch.setenv("CODEX_HOME", str(home))
    peer = codex_endpoint
    peer.respond(assistant_output("API key answer."))
    agent = Agent(
        instructions=INSTRUCTIONS,
        provider=NativeProvider(base_url=peer.url, api_key="existing-api-key"),
    )
    async with timeout(30), agent.session() as session:
        assert session.codex_account is None
        assert (await submit(session, "Hello.")).final_output == "API key answer."
        state = await session.save_state()
    assert state.codex_login_binding is None
    assert peer.request_headers[0]["authorization"] == "Bearer existing-api-key"
    assert "chatgpt-account-id" not in peer.request_headers[0]
    with pytest.raises(ValueError, match="same authentication mode"):
        async with codex_agent(home).session(state=state):
            pytest.fail("Restored API-key history in the plan-backed Session")
    assert len(peer.requests) == 1


@pytest.mark.parametrize("same_user", [True, False])
async def test_unauthorized_recovery_reloads_only_the_same_oauth_owner(
    monkeypatch, tmp_path, same_user
):
    home = saved_login(tmp_path / "codex")
    peer = ResponsesFixture()
    peer.respond(assistant_output("Recovered with renewed credentials."))
    seen = []

    async def handle(request: web.Request) -> web.StreamResponse:
        bearer = request.headers["Authorization"]
        seen.append(bearer)
        if bearer == "Bearer saved-token":
            saved_login(home, user=USER if same_user else "other-user", token="rotated-token")
            return web.json_response({"error": {"message": "Expired"}}, status=401)
        assert request.headers["ChatGPT-Account-ID"] == ACCOUNT
        return await peer.handle(request)

    monkeypatch.setenv("OPENAI_API_KEY", "never-use-this-key")
    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with TestServer(app) as server:
        monkeypatch.setattr(
            importlib.import_module("_agents_codex.session"),
            "_CODEX_RESPONSES_BASE_URL",
            str(server.make_url("/v1")),
        )
        async with timeout(30), codex_agent(home).session() as session:
            if same_user:
                result = await submit(session, "Recover from an expired access token.")
                assert result.final_output == "Recovered with renewed credentials."
            else:
                with pytest.raises(NativeError):
                    await submit(session, "Stop if the saved user changed.")
    expected = ["Bearer saved-token"]
    if same_user:
        expected.append("Bearer rotated-token")
    assert seen == expected


async def test_prompted_output_repair_keeps_the_saved_login(codex_endpoint, tmp_path):
    home = saved_login(tmp_path / "codex")
    peer = codex_endpoint
    peer.respond(assistant_output("invalid JSON"))
    peer.respond(assistant_output('{"count": 2}'))
    agent = codex_agent(home, output_type=PromptedOutput(dict[str, int], max_retries=1))
    async with timeout(30), agent.session() as session:
        result = await submit(session, "Report the number of matches.")
    assert result.final_output == {"count": 2}
    assert [headers["authorization"] for headers in peer.request_headers] == [
        "Bearer saved-token",
        "Bearer saved-token",
    ]


async def test_compaction_replacement_engine_keeps_the_original_oauth_owner(
    codex_endpoint, tmp_path
):
    home = saved_login(tmp_path / "codex")
    peer = codex_endpoint
    peer.respond(assistant_output("Initial context."))
    async with timeout(30), codex_agent(home).session() as session:
        assert (await submit(session, "Start.")).final_output == "Initial context."
        saved_login(home, user="other-user", token="wrong-token")
        with pytest.raises(RuntimeError, match="original Codex login"):
            await session.compact(summary="Retain the coding task.")
        assert len(peer.requests) == 1

        saved_login(home, token="rotated-token")
        await session.compact(summary="Retain the coding task.")
        peer.respond(assistant_output("After compaction."))
        assert (await submit(session, "Continue.")).final_output == "After compaction."
    assert peer.request_headers[1]["authorization"] == "Bearer rotated-token"
    assert peer.request_headers[1]["chatgpt-account-id"] == ACCOUNT


@pytest.mark.parametrize("profile", [{"id_account": "other-workspace"}, {"fedramp": True}])
async def test_saved_login_rejects_inconsistent_or_separate_backend_account(
    codex_endpoint, tmp_path, profile
):
    home = saved_login(tmp_path / "codex", **profile)
    with pytest.raises(RuntimeError, match="No usable saved Codex OAuth login"):
        async with codex_agent(home).session():
            pytest.fail("Opened with an unsupported account on the standard Codex backend")
    assert codex_endpoint.requests == []


@pytest.mark.parametrize("same_user", [True, False])
async def test_deferred_start_reopens_with_the_original_oauth_owner(
    codex_endpoint, tmp_path, same_user
):
    home = saved_login(tmp_path / "codex")
    peer = codex_endpoint
    backend = Provider([done()])
    jobs, tool = declared(Journal(), backend)
    agent = codex_agent(home, tools=[tool])
    async with timeout(30), agent.session() as session:
        request = await pending(session, tool, peer)
        saved_login(home, user=USER if same_user else "other-user", token="rotated-token")
        if same_user:
            peer.respond(assistant_output("Job finished."))
            async with jobs.start(session, request) as (_, turn):
                assert isinstance(turn, Turn)
                assert (await turn.get_result()).final_output == "Job finished."
        else:
            with pytest.raises(RuntimeError, match="original Codex login"):
                async with jobs.start(session, request):
                    pytest.fail("Started the job under a different saved user")
    assert backend.calls == ([("start", 2)] if same_user else [])
    assert [headers["authorization"] for headers in peer.request_headers] == (
        ["Bearer saved-token", "Bearer rotated-token"] if same_user else ["Bearer saved-token"]
    )
