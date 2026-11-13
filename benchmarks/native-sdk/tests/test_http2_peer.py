"""Exercise the optional TLS peer without loading either SDK."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import io
import json
import ssl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from aiohttp import ClientSession, TCPConnector

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from owned_process import defer_cleanup_signals, read_ready_line, terminate_group
from protocol_peer import final_text, main as peer_main
from worker import validate_transport


class PeerModeArgumentTests(unittest.TestCase):
    def test_h1_rejects_a_tls_port_its_readiness_does_not_report(self):
        with (
            mock.patch.object(sys, "argv", ["protocol_peer.py", "--tls-port", "0"]),
            mock.patch.object(sys, "stderr", io.StringIO()) as errors,
        ):
            with self.assertRaises(SystemExit):
                peer_main()
            self.assertIn("A custom TLS port requires dual HTTP mode", errors.getvalue())


@unittest.skipUnless(importlib.util.find_spec("hypercorn"), "Install requirements-http2-peer.txt")
class HTTP2PeerTests(unittest.TestCase):
    def test_h2_reuses_one_connection_and_h1_keeps_json_semantics(self):
        import httpx

        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-addext",
                    "subjectAltName=IP:127.0.0.1",
                ],
                capture_output=True,
                check=True,
                timeout=10,
            )
            process = None
            try:
                with defer_cleanup_signals():
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            str(Path(__file__).resolve().parents[1] / "protocol_peer.py"),
                            "--peer-http-mode",
                            "dual",
                            "--tls-port",
                            "0",
                            "--tls-cert",
                            str(cert),
                            "--tls-key",
                            str(key),
                            "--tools-per-round",
                            "0",
                            "--tool-rounds",
                            "0",
                            "--chunks",
                            "128",
                            "--output-chars",
                            "16384",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,
                    )
                ready = json.loads(read_ready_line(process))
                port = urlsplit(ready["base_url"]).port
                endpoint = f"https://127.0.0.1:{port}/v1/responses"
                trust = ssl.create_default_context(cafile=str(cert))

                async def exercise():
                    async with httpx.AsyncClient(
                        verify=trust, http2=True, trust_env=False
                    ) as client:
                        for index in range(2):
                            operation = f"h2-{index}"
                            response = await client.post(
                                endpoint,
                                json={
                                    "model": "fixture",
                                    "input": [
                                        {"role": "user", "content": f"BENCH_RUN={operation}"}
                                    ],
                                    "stream": True,
                                },
                            )
                            self.assertEqual(response.http_version, "HTTP/2")
                            response.raise_for_status()
                            events = [
                                json.loads(line[6:])
                                for line in response.text.splitlines()
                                if line.startswith("data: ")
                            ]
                            self.assertEqual(events[-1]["type"], "response.completed")
                            deltas = [
                                event["delta"]
                                for event in events
                                if event["type"] == "response.output_text.delta"
                            ]
                            self.assertEqual(len(deltas), 128)
                            self.assertEqual("".join(deltas), final_text(operation, 16384))
                    async with httpx.AsyncClient(
                        verify=trust, http2=False, trust_env=False
                    ) as client:
                        response = await client.post(
                            endpoint,
                            json={
                                "model": "fixture",
                                "input": [{"role": "user", "content": "BENCH_RUN=h1-json"}],
                                "stream": False,
                            },
                        )
                        self.assertEqual(response.http_version, "HTTP/1.1")
                        response.raise_for_status()
                        self.assertEqual(
                            response.json()["output"][0]["content"][0]["text"],
                            final_text("h1-json", 16384),
                        )
                    async with ClientSession(
                        connector=TCPConnector(ssl=trust), trust_env=False
                    ) as client:
                        async with client.ws_connect(endpoint) as socket:
                            await socket.send_json(
                                {
                                    "type": "response.create",
                                    "store": False,
                                    "model": "fixture",
                                    "input": [{"role": "user", "content": "BENCH_RUN=ws-local"}],
                                }
                            )
                            while True:
                                event = await socket.receive_json(timeout=5)
                                if event["type"] == "response.completed":
                                    self.assertEqual(
                                        event["response"]["output"][0]["content"][0]["text"],
                                        final_text("ws-local", 16384),
                                    )
                                    break
                    async with httpx.AsyncClient(trust_env=False) as client:
                        stats = (
                            await client.get(ready["control_base_url"][:-3] + "/stats?retired=1")
                        ).json()
                    self.assertEqual(stats["errors"], 0)
                    self.assertEqual(stats["requests"], 4)
                    native = [row for row in stats["receipts"] if row["op_id"].startswith("h2-")]
                    self.assertEqual(len({row["tls_connection_id"] for row in native}), 1)
                    self.assertTrue(
                        all(
                            row["response_body_complete"]
                            for row in stats["receipts"]
                            if row["wire_transport"] != "websocket"
                        )
                    )
                    classic = next(row for row in stats["receipts"] if row["op_id"] == "h1-json")
                    self.assertEqual(
                        (classic["http_version"], classic["tls_alpn_protocol"]), ("1.1", "http/1.1")
                    )
                    native_stats = {
                        **stats,
                        "requests": 2,
                        "receipts": native,
                        "emitted_text_chunks": 256,
                        "tls_transports": {
                            str(native[0]["tls_connection_id"]): stats["tls_transports"][
                                str(native[0]["tls_connection_id"])
                            ]
                        },
                    }
                    workload = SimpleNamespace(
                        concurrency=1,
                        prefill=0,
                        iterations=2,
                        tool_rounds=0,
                        chunks=128,
                        stream=False,
                    )
                    validate_transport(
                        workload,
                        native_stats,
                        engine="v1",
                        response_mode="streaming",
                        peer_http_mode="dual",
                    )
                    for field, invalid in [
                        ("http_version", "1.1"),
                        ("tls_alpn_protocol", None),
                        ("response_body_complete", False),
                    ]:
                        with self.subTest(field=field):
                            changed = copy.deepcopy(native_stats)
                            changed["receipts"][0][field] = invalid
                            with self.assertRaises(ValueError):
                                validate_transport(
                                    workload,
                                    changed,
                                    engine="v1",
                                    response_mode="streaming",
                                    peer_http_mode="dual",
                                )

                asyncio.run(exercise())
            finally:
                if process is not None:
                    primary = sys.exception()
                    try:
                        terminate_group(process, term_timeout=5)
                        stderr = process.stderr.read(65536)
                        if primary is not None and stderr:
                            primary.add_note("Joined peer stderr: " + stderr)
                        if primary is None:
                            self.assertEqual(process.returncode, 0, stderr)
                    finally:
                        process.stdout.close()
                        process.stderr.close()


if __name__ == "__main__":
    unittest.main()
