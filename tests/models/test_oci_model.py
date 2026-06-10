from __future__ import annotations

from typing import Any

import httpx
import pytest

from agents.exceptions import UserError
from agents.extensions.models.oci_provider import OCIProvider
from agents.extensions.models.oci_signer import (
    OCIClientConfig,
    OCIRequestSigner,
    oci_openai_base_url,
)

COMPARTMENT_ID = "ocid1.compartment.oc1..testcompartment"


class FakeSigner:
    """Stands in for an OCI signer; records what it signed."""

    def __init__(self, signature: str = "Signature test") -> None:
        self.signature = signature
        self.signed_bodies: list[Any] = []

    def do_request_sign(self, prepared: Any) -> None:
        self.signed_bodies.append(prepared.body)
        prepared.headers["authorization"] = self.signature
        prepared.headers["date"] = "Mon, 01 Jan 2026 00:00:00 GMT"


def _drive_auth_flow(signer: OCIRequestSigner, request: httpx.Request) -> httpx.Request:
    flow = signer.auth_flow(request)
    return next(flow)


def test_endpoint_construction() -> None:
    assert (
        oci_openai_base_url("us-chicago-1")
        == "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1"
    )


def test_signer_replaces_bearer_auth_and_adds_compartment() -> None:
    fake_signer = FakeSigner()
    signer = OCIRequestSigner(fake_signer, compartment_id=COMPARTMENT_ID)
    request = httpx.Request(
        "POST",
        "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1/chat/completions",
        json={"model": "openai.gpt-4o"},
        headers={"Authorization": "Bearer should-be-removed"},
    )

    signed = _drive_auth_flow(signer, request)

    assert signed.headers["authorization"] == "Signature test"
    assert signed.headers["opc-compartment-id"] == COMPARTMENT_ID
    assert fake_signer.signed_bodies == [request.content]


def test_signer_rebuilds_signer_on_401() -> None:
    rebuilt: list[FakeSigner] = []

    def refresh() -> FakeSigner:
        new_signer = FakeSigner(signature=f"Signature refreshed-{len(rebuilt)}")
        rebuilt.append(new_signer)
        return new_signer

    signer = OCIRequestSigner(FakeSigner(), refresh_signer=refresh)
    request = httpx.Request("POST", "https://example.com/openai/v1/chat/completions", json={})

    flow = signer.auth_flow(request)
    first = next(flow)
    assert first.headers["authorization"] == "Signature test"

    retried = flow.send(httpx.Response(401, request=request))
    assert len(rebuilt) == 1
    assert retried.headers["authorization"] == "Signature refreshed-0"


def test_signer_does_not_retry_without_refresh() -> None:
    signer = OCIRequestSigner(FakeSigner())
    request = httpx.Request("POST", "https://example.com/openai/v1/chat/completions", json={})

    flow = signer.auth_flow(request)
    next(flow)
    with pytest.raises(StopIteration):
        flow.send(httpx.Response(401, request=request))


def test_provider_requires_model_name() -> None:
    provider = OCIProvider(compartment_id=COMPARTMENT_ID)
    with pytest.raises(UserError):
        provider.get_model(None)


def test_provider_routes_to_model_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents.extensions.models.oci_provider as oci_provider_module

    client_config = OCIClientConfig(
        signer=FakeSigner(),
        config={},
        region="us-chicago-1",
        compartment_id=COMPARTMENT_ID,
    )
    monkeypatch.setattr(
        oci_provider_module, "resolve_client_config", lambda **kwargs: client_config
    )

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
        oci_provider_module,
        "build_signed_openai_client",
        lambda config, request_timeout: object(),
    )

    provider = OCIProvider(compartment_id=COMPARTMENT_ID)
    provider.get_model("openai.gpt-4o")
    provider.get_model("responses:openai.gpt-5")

    assert created == [
        ("chat", "openai.gpt-4o"),
        ("responses", "openai.gpt-5"),
    ]
