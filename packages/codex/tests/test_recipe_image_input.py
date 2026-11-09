"""Check inline PNG transport and settled validation, not visual reasoning."""

import base64
import importlib
import json
import sys
import time
from pathlib import Path

import pytest

from agents import NativeError, OutputValidationError
from agents.testing import ScriptedResponses, text
from examples.codex.recipes.image_input import image_agent, image_message, write_synthetic_png
from tests._asyncio import timeout


async def test_synthetic_local_png_reaches_request_with_text_and_typed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "red.png"
    write_synthetic_png(path)
    started = time.perf_counter()
    native_imports = []
    original_import = importlib.import_module

    def timed_import(name, package=None):
        if name != "agents._native":
            return original_import(name, package)
        cold = name not in sys.modules
        imported = time.perf_counter()
        try:
            return original_import(name, package)
        finally:
            native_imports.append({"cold": cold, "seconds": time.perf_counter() - imported})

    monkeypatch.setattr(importlib, "import_module", timed_import)
    async with (
        ScriptedResponses([text('{"dominant_color":"red","confidence":1.0}')]) as peer,
        timeout(20),
    ):
        run_started = time.perf_counter()
        try:
            result = await image_agent(peer.provider).run(image_message(path))
        except BaseException:
            failed = time.perf_counter()
            print(
                "image-input-startup-diagnostic: "
                + json.dumps(
                    {
                        "budget_elapsed_seconds": failed - started,
                        "sdk_run_elapsed_including_cleanup_seconds": failed - run_started,
                        "native_imports": native_imports,
                        "peer_request_count": len(peer.requests),
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
            raise
        assert result.final_output.dominant_color == "red"
        assert result.final_output.confidence == 1
        [message] = [
            item
            for item in peer.requests[0]["input"]
            if item.get("role") == "user"
            and any(part.get("type") == "input_image" for part in item.get("content", []))
        ]
        content = message["content"]
        assert len(content) == 2
        assert content[0] == {
            "type": "input_text",
            "text": "Name the dominant color in this synthetic image.",
        }
        assert content[1]["type"] == "input_image"
        header, payload = content[1]["image_url"].split(",", 1)
        assert header == "data:image/png;base64"
        assert base64.b64decode(payload) == path.read_bytes()
        assert peer.requests[0]["text"]["format"]["type"] == "json_schema"


async def test_invalid_settled_image_result_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "red.png"
    write_synthetic_png(path)
    started = time.perf_counter()
    async with (
        ScriptedResponses([text('{"dominant_color":"red","confidence":9}')]) as peer,
        timeout(20),
    ):
        run_started = time.perf_counter()
        try:
            with pytest.raises(OutputValidationError):
                await image_agent(peer.provider).run(image_message(path))
            assert len(peer.requests) == 1
        except BaseException:
            failed = time.perf_counter()
            print(
                "invalid-image-startup-diagnostic: "
                + json.dumps(
                    {
                        "fixture_setup_seconds": run_started - started,
                        "sdk_run_elapsed_including_cleanup_seconds": failed - run_started,
                        "total_elapsed_seconds": failed - started,
                        "peer_request_count": len(peer.requests),
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
            raise


@pytest.mark.parametrize(
    ("reference", "error", "message"),
    [
        ("https://example.com/image.png", NativeError, "cannot preserve provider image references"),
        ("/tmp/image.png", ValueError, "absolute HTTP\\(S\\) or image data URL"),
        ("file_synthetic", ValueError, "absolute HTTP\\(S\\) or image data URL"),
    ],
)
async def test_unsupported_image_references_fail_before_model_request(
    reference: str, error: type[Exception], message: str
) -> None:
    async with ScriptedResponses() as peer, timeout(20):
        with pytest.raises(error, match=message):
            await image_agent(peer.provider).run(
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": reference},
                    ],
                }
            )
        assert peer.requests == ()
