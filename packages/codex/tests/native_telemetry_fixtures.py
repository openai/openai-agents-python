"""Temporary CA-signed identities for native telemetry collector tests."""

from __future__ import annotations

import ssl
from pathlib import Path

from agents import telemetry
from tests.native_executor_fixtures import _openssl, test_certificate as create_server_certificate


async def mutual_tls_collector(root: Path) -> tuple[telemetry.OtelTlsConfig, ssl.SSLContext]:
    ca, context = await create_server_certificate(root, "DNS:localhost,IP:127.0.0.1")
    certificate, key, request = root / "client.pem", root / "client.key", root / "client.csr"
    await _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(request),
        "-subj",
        "/CN=Synthetic telemetry client",
    )
    extensions = root / "client.cnf"
    extensions.write_text("extendedKeyUsage=clientAuth\n")
    await _openssl(
        "x509",
        "-req",
        "-in",
        str(request),
        "-CA",
        str(ca),
        "-CAkey",
        str(root / "ca.key"),
        "-CAcreateserial",
        "-out",
        str(certificate),
        "-days",
        "1",
        "-extfile",
        str(extensions),
    )
    context.load_verify_locations(cafile=ca)
    context.verify_mode = ssl.CERT_REQUIRED
    return telemetry.OtelTlsConfig(
        ca_certificate=ca, client_certificate=certificate, client_private_key=key
    ), context
