from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest

from agents.exceptions import UserError
from agents.extensions.models.oci_provider import OCIProvider

COMPARTMENT_ID = "ocid1.compartment.oc1..testcompartment"
PROJECT_ID = "ocid1.generativeaiproject.oc1..testproject"


class FakeOciAuth(httpx.Auth):
    """Stands in for the oci-openai auth implementations."""

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["authorization"] = "Signature test"
        yield request


class FakeAsyncOpenAI:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_builder_constructs_signed_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents.extensions.models.oci_model as oci_model_module
    from agents.extensions.models.oci_model import build_signed_openai_client

    monkeypatch.setattr(oci_model_module, "_load_profile", lambda profile, config_file: {})
    monkeypatch.setattr(oci_model_module, "_build_auth", lambda *args, **kwargs: FakeOciAuth())

    client = build_signed_openai_client(
        region="us-chicago-1",
        compartment_id=COMPARTMENT_ID,
        project_id=PROJECT_ID,
    )
    assert "inference.generativeai.us-chicago-1.oci.oraclecloud.com" in str(client.base_url)
    assert client.project == PROJECT_ID
    # The compartment header is attached by the oci-openai client.
    assert client._client.headers.get("opc-compartment-id") == COMPARTMENT_ID


def test_builder_resolves_project_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents.extensions.models.oci_model as oci_model_module
    from agents.extensions.models.oci_model import build_signed_openai_client

    monkeypatch.setattr(oci_model_module, "_load_profile", lambda profile, config_file: {})
    monkeypatch.setattr(oci_model_module, "_build_auth", lambda *args, **kwargs: FakeOciAuth())
    monkeypatch.setenv("OCI_PROJECT_ID", PROJECT_ID)

    client = build_signed_openai_client(region="us-chicago-1", compartment_id=COMPARTMENT_ID)
    assert client.project == PROJECT_ID


def test_builder_requires_compartment(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents.extensions.models.oci_model as oci_model_module
    from agents.extensions.models.oci_model import build_signed_openai_client

    monkeypatch.setattr(oci_model_module, "_load_profile", lambda profile, config_file: {})
    monkeypatch.setattr(oci_model_module, "_build_auth", lambda *args, **kwargs: FakeOciAuth())
    monkeypatch.delenv("OCI_COMPARTMENT_ID", raising=False)

    with pytest.raises(UserError):
        build_signed_openai_client(region="us-chicago-1")


def test_auth_mode_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents.extensions.models.oci_model as oci_model_module
    from agents.extensions.models.oci_model import _build_auth

    selected: list[tuple[str, dict[str, Any]]] = []

    def make_stub(kind: str) -> Any:
        def factory(**kwargs: Any) -> Any:
            selected.append((kind, kwargs))
            return FakeOciAuth()

        return factory

    monkeypatch.setattr(oci_model_module, "OciUserPrincipalAuth", make_stub("api_key"))
    monkeypatch.setattr(oci_model_module, "OciSessionAuth", make_stub("security_token"))

    # Profiles with a security token use session auth automatically.
    _build_auth(None, "PROFILE_A", None, {"security_token_file": "/tmp/token"})
    # Plain API-key profiles use user-principal auth.
    _build_auth(None, "PROFILE_B", None, {})
    # Explicit modes are honored regardless of the profile contents.
    _build_auth("security_token", "PROFILE_C", None, {})

    assert [kind for kind, _ in selected] == ["security_token", "api_key", "security_token"]
    assert selected[0][1]["profile_name"] == "PROFILE_A"


async def test_close_releases_internally_created_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents.extensions.models.oci_model as oci_model_module

    fake_client = FakeAsyncOpenAI()
    monkeypatch.setattr(
        oci_model_module, "build_signed_openai_client", lambda **kwargs: fake_client
    )

    from agents.extensions.models.oci_model import OCIChatCompletionsModel, OCIResponsesModel

    model = OCIChatCompletionsModel("openai.gpt-4o")
    await model.close()
    assert fake_client.closed

    fake_client = FakeAsyncOpenAI()
    monkeypatch.setattr(
        oci_model_module, "build_signed_openai_client", lambda **kwargs: fake_client
    )
    responses_model = OCIResponsesModel("openai.gpt-5")
    await responses_model.close()
    assert fake_client.closed


async def test_close_leaves_caller_provided_client_open() -> None:
    from agents.extensions.models.oci_model import OCIChatCompletionsModel

    caller_client = FakeAsyncOpenAI()
    model = OCIChatCompletionsModel("openai.gpt-4o", openai_client=caller_client)  # type: ignore[arg-type]
    await model.close()
    assert not caller_client.closed


def test_provider_requires_model_name() -> None:
    provider = OCIProvider(compartment_id=COMPARTMENT_ID)
    with pytest.raises(UserError):
        provider.get_model(None)


def test_provider_routes_to_model_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents.extensions.models.oci_provider as oci_provider_module

    created: list[tuple[str, str]] = []

    class StubChatModel:
        def __init__(self, model: str, **kwargs: Any) -> None:
            created.append(("chat", model))

    class StubResponsesModel:
        def __init__(self, model: str, **kwargs: Any) -> None:
            created.append(("responses", model))

    monkeypatch.setattr(oci_provider_module, "OCIChatCompletionsModel", StubChatModel)
    monkeypatch.setattr(oci_provider_module, "OCIResponsesModel", StubResponsesModel)
    monkeypatch.setattr(
        oci_provider_module, "build_signed_openai_client", lambda **kwargs: object()
    )

    provider = OCIProvider(compartment_id=COMPARTMENT_ID)
    provider.get_model("openai.gpt-4o")
    provider.get_model("responses:openai.gpt-5")

    assert created == [
        ("chat", "openai.gpt-4o"),
        ("responses", "openai.gpt-5"),
    ]
