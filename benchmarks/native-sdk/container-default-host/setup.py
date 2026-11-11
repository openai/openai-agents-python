"""Prepare default-host trust inside a disposable, root-owned Linux container.

Provision matching Python 3.13 SDK/peer virtualenvs before setup; no SDK install here.
Launch with docker run --network none --add-host api.openai.com:127.0.0.1 ...
Then: python3 setup.py --directory /run/sdk-benchmark --python /opt/sdk/bin/python
      --python /opt/peer/bin/python
Pass the emitted setup.json to --default-host-setup; the TLS peer listens on 443.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import uuid
from pathlib import Path


def run(*command):
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, required=True, help="Fresh container-only asset directory"
    )
    parser.add_argument(
        "--python",
        type=Path,
        action="append",
        required=True,
        help="Preinstalled SDK/peer interpreter; repeat for every virtualenv",
    )
    args = parser.parse_args()
    marker = next(
        (
            p
            for p in (Path("/.dockerenv"), Path("/run/.containerenv"))
            if p.is_file() and not p.is_symlink() and p.stat().st_uid == 0
        ),
        None,
    )
    if os.getuid() != 0 or os.geteuid() != 0 or marker is None:
        parser.error("Setup requires root and a root-owned container marker")
    forbidden = (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "WS_PROXY",
        "WSS_PROXY",
        "SOCKS_PROXY",
        "ws_proxy",
        "wss_proxy",
        "socks_proxy",
        "NO_PROXY",
        "no_proxy",
        "REQUEST_METHOD",
        "CODEX_SANDBOX",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "LD_PRELOAD",
        "LD_AUDIT",
        "OPENSSL_CONF",
        "OPENSSL_MODULES",
    )
    if any(name in os.environ for name in forbidden):
        parser.error(
            "Unset CA, proxy, endpoint, preload and OpenSSL overrides, including empty values"
        )
    addresses = sorted(
        {row[4][0] for row in socket.getaddrinfo("api.openai.com", 443, type=socket.SOCK_STREAM)}
    )
    if addresses != ["127.0.0.1"]:
        parser.error("api.openai.com must resolve exclusively to 127.0.0.1; use Docker --add-host")
    interpreters = [value.absolute() for value in args.python]  # Preserve virtualenv symlinks.
    bundles = {}
    for interpreter in interpreters:
        bundle = Path(
            run(str(interpreter), "-I", "-c", "import certifi; print(certifi.where())")
        ).resolve(strict=True)
        if not bundle.is_file():
            parser.error("Each interpreter must provide a real certifi bundle")
        bundles.setdefault(bundle, []).append(str(interpreter))
    os.umask(0o077)
    directory = args.directory.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    ca, ca_key = directory / "ca.pem", directory / "ca.key"
    cert, key, csr, extensions = (
        directory / name for name in ("server.pem", "server.key", "server.csr", "server.ext")
    )
    run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-config",
        "/dev/null",
        "-subj",
        "/CN=SDK benchmark local CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca),
    )
    run(
        "openssl",
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-config",
        "/dev/null",
        "-subj",
        "/CN=api.openai.com",
        "-keyout",
        str(key),
        "-out",
        str(csr),
    )
    extensions.write_text(
        "subjectAltName=DNS:api.openai.com\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
    )
    run(
        "openssl",
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-days",
        "1",
        "-extfile",
        str(extensions),
        "-out",
        str(cert),
    )
    run("openssl", "verify", "-CAfile", str(ca), "-verify_hostname", "api.openai.com", str(cert))
    installed = Path("/usr/local/share/ca-certificates") / f"sdk-benchmark-{uuid.uuid4().hex}.crt"
    installed.write_bytes(ca.read_bytes())
    installed.chmod(0o644)
    run("update-ca-certificates")
    run("openssl", "verify", "-verify_hostname", "api.openai.com", str(cert))
    certifi_receipts = {}
    addition = b"\n" + ca.read_bytes()
    for bundle, owners in bundles.items():
        before = bundle.read_bytes()
        with bundle.open("ab") as output:
            output.write(addition)
        after = bundle.read_bytes()
        if after != before + addition:
            raise RuntimeError("certifi bundle bytes changed beyond the explicit CA append")
        certifi_receipts[str(bundle)] = {
            "interpreters": owners,
            "before_sha256": sha(before),
            "after_sha256": sha(after),
        }
    assets = (ca, ca_key, cert, key, installed)
    setup = {
        "api_origin": "https://api.openai.com/v1",
        "tls_cert": str(cert),
        "tls_key": str(key),
        "files": {
            **{str(path): sha(path.read_bytes()) for path in assets},
            **{name: receipt["after_sha256"] for name, receipt in certifi_receipts.items()},
        },
        "synthetic_ca": str(ca),
        "installed_ca": str(installed),
        "certifi": certifi_receipts,
        "container_marker": str(marker),
        "uid": os.geteuid(),
        "resolved_addresses": addresses,
        "fixture_kind": "container-default-host-v1",
        "setup_source_sha256": sha(Path(__file__).read_bytes()),
        "certifi_snapshot_scope": "after system CA installation; before explicit append",
        "network_mode_requirement": "Docker --network none (caller supplied)",
        "verification_bypass": False,
    }
    destination = directory / "setup.json"
    destination.write_text(json.dumps(setup, indent=2) + "\n")
    print(destination)


if __name__ == "__main__":
    main()
