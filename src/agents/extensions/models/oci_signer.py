"""OCI request-signing support shared by the OCI Generative AI model classes.

Oracle Cloud Infrastructure (OCI) authenticates HTTP requests with per-request
signatures derived from IAM credentials rather than bearer tokens. This module
resolves those credentials (API key, session token, instance principal, or
resource principal) and exposes an `httpx.Auth` hook that signs each outgoing
request, so the OpenAI client can talk to the OCI Generative AI
OpenAI-compatible endpoints.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

try:
    import oci
except ImportError as _e:
    raise ImportError(
        "`oci` is required to use the OCI model classes. You can install it via the optional "
        "dependency group: `pip install 'openai-agents[oci]'`."
    ) from _e

import requests

from ...exceptions import UserError

DEFAULT_OCI_REGION = "us-chicago-1"
"""Fallback region used when none is configured anywhere else."""

OCIAuthType = Literal["api_key", "security_token", "instance_principal", "resource_principal"]


def oci_openai_base_url(region: str) -> str:
    """Return the OCI Generative AI OpenAI-compatible base URL for a region."""
    return f"https://inference.generativeai.{region}.oci.oraclecloud.com/openai/v1"


@dataclass
class OCIClientConfig:
    """Resolved OCI credentials and routing information.

    Attributes:
        signer: The OCI signer used to sign requests.
        config: The OCI SDK config dict (empty for principal-based auth).
        region: The region whose Generative AI endpoint should be called.
        compartment_id: The compartment all inference requests are billed against.
        refresh_signer: Optional zero-arg callable that rebuilds the signer when its
            credentials expire (session tokens, principal tokens). `None` for API keys,
            which do not expire.
    """

    signer: Any
    config: dict[str, Any]
    region: str
    compartment_id: str | None
    refresh_signer: Callable[[], Any] | None = None


def _load_file_config(profile: str | None, config_file: str | None) -> dict[str, Any]:
    file_location = config_file or oci.config.DEFAULT_LOCATION
    profile_name = profile or os.environ.get("OCI_CLI_PROFILE") or oci.config.DEFAULT_PROFILE
    config: dict[str, Any] = oci.config.from_file(
        file_location=file_location, profile_name=profile_name
    )
    return config


def _build_api_key_signer(config: dict[str, Any]) -> Any:
    return oci.signer.Signer(
        tenancy=config["tenancy"],
        user=config["user"],
        fingerprint=config["fingerprint"],
        private_key_file_location=config["key_file"],
        pass_phrase=config.get("pass_phrase"),
    )


def _build_security_token_signer(config: dict[str, Any]) -> Any:
    token = Path(config["security_token_file"]).expanduser().read_text().strip()
    private_key = oci.signer.load_private_key_from_file(
        config["key_file"], config.get("pass_phrase")
    )
    return oci.auth.signers.SecurityTokenSigner(token=token, private_key=private_key)


def _build_instance_principal_signer() -> Any:
    return oci.auth.signers.InstancePrincipalsSecurityTokenSigner()


def _build_resource_principal_signer() -> Any:
    return oci.auth.signers.get_resource_principals_signer()


def resolve_client_config(
    *,
    auth_type: OCIAuthType | None = None,
    profile: str | None = None,
    config_file: str | None = None,
    region: str | None = None,
    compartment_id: str | None = None,
) -> OCIClientConfig:
    """Resolve OCI credentials into a signer plus routing information.

    When `auth_type` is omitted, file-based configuration is used and the auth mode is
    inferred: profiles carrying a `security_token_file` use session-token signing,
    everything else uses API-key signing. Principal-based modes must be requested
    explicitly because they cannot be detected from a config file.

    Resolution order for the region: explicit argument, `OCI_REGION` env var, the config
    file profile's `region`, then the service default. For the compartment: explicit
    argument, `OCI_COMPARTMENT_ID` env var, then the profile's tenancy as a best-effort
    fallback.
    """
    refresh: Callable[[], Any] | None = None

    if auth_type == "instance_principal":
        signer = _build_instance_principal_signer()
        config: dict[str, Any] = {}
        refresh = _build_instance_principal_signer
    elif auth_type == "resource_principal":
        signer = _build_resource_principal_signer()
        config = {}
        refresh = _build_resource_principal_signer
    else:
        config = _load_file_config(profile, config_file)
        use_session_token = auth_type == "security_token" or (
            auth_type is None and config.get("security_token_file")
        )
        if use_session_token:
            if not config.get("security_token_file"):
                raise UserError(
                    "auth_type='security_token' requires a `security_token_file` entry in the "
                    "selected OCI config profile."
                )
            signer = _build_security_token_signer(config)

            def _refresh_from_disk(cfg: dict[str, Any] = config) -> Any:
                return _build_security_token_signer(cfg)

            refresh = _refresh_from_disk
        else:
            signer = _build_api_key_signer(config)

    resolved_region = (
        region
        or os.environ.get("OCI_REGION")
        or config.get("region")
        or getattr(signer, "region", None)
        or DEFAULT_OCI_REGION
    )
    resolved_compartment = (
        compartment_id or os.environ.get("OCI_COMPARTMENT_ID") or config.get("tenancy")
    )

    return OCIClientConfig(
        signer=signer,
        config=config,
        region=str(resolved_region),
        compartment_id=resolved_compartment,
        refresh_signer=refresh,
    )


class OCIRequestSigner(httpx.Auth):
    """`httpx.Auth` hook that applies OCI request signing to every request.

    The hook strips any bearer auth injected by the OpenAI client, attaches the
    `opc-compartment-id` header expected by the OCI OpenAI-compatible endpoints, signs
    the request with the configured signer, and transparently rebuilds expiring signers
    both on a timed interval and on a 401 response.
    """

    requires_request_body = True

    def __init__(
        self,
        signer: Any,
        *,
        compartment_id: str | None = None,
        refresh_signer: Callable[[], Any] | None = None,
        refresh_interval: float = 600.0,
    ) -> None:
        self._signer = signer
        self._compartment_id = compartment_id
        self._refresh_signer = refresh_signer
        self._refresh_interval = refresh_interval
        self._last_refresh = time.monotonic()
        self._lock = threading.Lock()

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        self._maybe_refresh()
        self._sign(request)
        response = yield request
        if response.status_code == 401 and self._refresh_signer is not None:
            self._refresh(force=True)
            self._sign(request)
            yield request

    def _maybe_refresh(self) -> None:
        if self._refresh_signer is None:
            return
        if time.monotonic() - self._last_refresh >= self._refresh_interval:
            self._refresh(force=False)

    def _refresh(self, *, force: bool) -> None:
        if self._refresh_signer is None:
            return
        with self._lock:
            if not force and time.monotonic() - self._last_refresh < self._refresh_interval:
                return
            self._signer = self._refresh_signer()
            self._last_refresh = time.monotonic()

    def _sign(self, request: httpx.Request) -> None:
        # The OpenAI client always sends a bearer token; OCI uses signature auth instead.
        request.headers.pop("Authorization", None)
        if self._compartment_id is not None:
            request.headers["opc-compartment-id"] = self._compartment_id

        try:
            content = request.content
        except httpx.RequestNotRead:
            content = request.read()

        # OCI signers operate on `requests.PreparedRequest`; rebuild the request in that
        # shape, sign it, and copy the signature headers (authorization, date, host,
        # x-content-sha256, ...) back onto the httpx request.
        prepared = requests.Request(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            data=content,
        ).prepare()
        self._signer.do_request_sign(prepared)
        for key, value in prepared.headers.items():
            request.headers[key] = value


__all__ = [
    "DEFAULT_OCI_REGION",
    "OCIAuthType",
    "OCIClientConfig",
    "OCIRequestSigner",
    "oci_openai_base_url",
    "resolve_client_config",
]
