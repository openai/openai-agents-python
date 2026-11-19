from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from agents.sandbox import Manifest
from agents.sandbox.entries import Dir, File
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient


def test_inline_binary_file_round_trips_through_nested_manifest_json() -> None:
    content = bytes(range(256))
    manifest = Manifest(entries={"data": Dir(children={"blob.bin": File(content=content)})})

    payload = manifest.model_dump(mode="json")
    assert payload["entries"]["data"]["children"]["blob.bin"]["content"] == {
        "type": "base64",
        "data": base64.b64encode(content).decode("ascii"),
    }
    restored = Manifest.model_validate_json(json.dumps(payload))
    directory = restored.entries["data"]
    assert isinstance(directory, Dir)
    blob = directory.children["blob.bin"]
    assert isinstance(blob, File)
    assert blob.content == content
    assert restored.model_dump(mode="json") == payload


def test_text_file_manifest_json_keeps_the_released_format() -> None:
    content = "café\n".encode()
    manifest = Manifest(entries={"note.txt": File(content=content)})
    payload = manifest.model_dump(mode="json")
    assert payload["entries"]["note.txt"]["content"] == "café\n"
    legacy = json.loads(json.dumps(payload))
    legacy["entries"]["note.txt"]["content"] = content.decode()
    restored = Manifest.model_validate(legacy).entries["note.txt"]
    assert isinstance(restored, File)
    assert restored.content == content
    assert manifest.entries["note.txt"].model_dump()["content"] == content


@pytest.mark.parametrize(
    "value",
    [
        {"type": "unknown", "data": "AA=="},
        {"type": "base64", "data": 1},
        {"type": "base64", "data": "not base64!"},
        {"type": "base64", "data": "AA==", "extra": True},
    ],
)
def test_inline_file_rejects_invalid_encoded_content(value: object) -> None:
    with pytest.raises(ValidationError):
        File.model_validate({"content": value})


def test_inline_file_json_schemas_declare_the_encoded_object() -> None:
    for mode in ("validation", "serialization"):
        schema = File.model_json_schema(mode=mode)
        choices = schema["properties"]["content"]["anyOf"]
        assert any(choice.get("type") == "string" for choice in choices)
        encoded = next(choice for choice in choices if "$ref" in choice)
        definition = schema["$defs"][encoded["$ref"].rsplit("/", 1)[1]]
        assert definition["additionalProperties"] is False
        assert set(definition["required"]) == {"type", "data"}
        assert definition["properties"]["type"]["const"] == "base64"
        assert definition["properties"]["data"]["type"] == "string"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="Unix-local requires a POSIX host")
async def test_inline_binary_file_survives_real_session_serialization_and_resume(
    tmp_path: Path,
) -> None:
    client = UnixLocalSandboxClient(inherit_host_environment=False)
    content = b"SQLite format 3\x00\x80\xff\x00"
    manifest = Manifest(
        root=str(tmp_path / "workspace"), entries={"data.db": File(content=content)}
    )
    async with await client.create(manifest=manifest) as session:
        assert (tmp_path / "workspace/data.db").read_bytes() == content
        state = session.state
    payload = json.loads(json.dumps(client.serialize_session_state(state)))
    restored = client.deserialize_session_state(payload)
    async with await client.resume(restored):
        assert (tmp_path / "workspace/data.db").read_bytes() == content
