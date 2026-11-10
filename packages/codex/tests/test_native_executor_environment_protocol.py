"""Real receiver and child-process checks for literal environment names."""

import base64
import json
import ssl
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ORDINARY_ENV_CONTROL", "literal-environment-value"),
        ("$serde_json::private::Number", "7301592846"),
        ("$serde_json::private::RawValue", '{"APP_MODE":"injected-env-marker"}'),
        ("__proto__", "literal-environment-value"),
    ],
)
async def test_literal_names_reach_real_child(tmp_path: Path, name: str, value: str) -> None:
    async with ExecutorPeer(tmp_path / "remote").serve() as peer:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.load_verify_locations(cafile=str(peer.ca))
        assert tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
            async with client.ws_connect(
                peer.url,
                ssl=tls,
                headers={"Authorization": "Bearer " + peer.token},
            ) as socket:
                request_id = 0

                async def rpc(method: str, params: dict[str, Any]) -> Any:
                    nonlocal request_id
                    request_id += 1
                    await socket.send_json({"id": request_id, "method": method, "params": params})
                    async with timeout(10):
                        while True:
                            response = await socket.receive_json()
                            if response.get("id") == request_id:
                                assert "error" not in response, response
                                return response["result"]

                await rpc(
                    "initialize", {"clientName": "literal-env-audit", "resumeSessionId": None}
                )
                await socket.send_json({"method": "initialized", "params": {}})
                await rpc(
                    "process/start",
                    {
                        "processId": "literal-env",
                        "argv": [
                            sys.executable,
                            "-c",
                            "import json, os, sys\n"
                            "print(json.dumps({\n"
                            "    'observed': os.environ.get(sys.argv[1]),\n"
                            "    'promoted': os.environ.get('APP_MODE'),\n"
                            "}))",
                            name,
                        ],
                        "cwd": peer.root.as_uri(),
                        "envPolicy": {
                            "inherit": "none",
                            "ignoreDefaultExcludes": False,
                            "exclude": [],
                            "set": {name: value},
                            "includeOnly": [],
                        },
                        "env": {},
                        "tty": False,
                        "arg0": None,
                        "sandbox": None,
                    },
                )
                output = bytearray()
                next_seq = None
                for _ in range(10):
                    result = await rpc(
                        "process/read",
                        {
                            "processId": "literal-env",
                            "afterSeq": next_seq,
                            "maxBytes": 4096,
                            "waitMs": 1000,
                        },
                    )
                    for chunk in result["chunks"]:
                        output.extend(base64.b64decode(chunk["chunk"]))
                    next_seq = result["nextSeq"]
                    if result["exited"]:
                        assert result["exitCode"] == 0, (
                            f"captured child stdout/stderr: {output.decode(errors='replace')}"
                            if name == "ORDINARY_ENV_CONTROL"
                            else ""
                        )
                        break
                else:
                    pytest.fail("Child process did not exit")
                assert json.loads(output) == {"observed": value, "promoted": None}
