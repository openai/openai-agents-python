from __future__ import annotations

import base64
import gzip
import io
import mimetypes

import pytest

from agents.sandbox.capabilities.tools import ViewImageArgs, ViewImageTool
from agents.testing import scripted_sandbox_session
from agents.tool import ToolOutputImage

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a84QAAAAASUVORK5CYII="
)
_SVG_TEXT = '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
_SVG_BODY = _SVG_TEXT.encode()


@pytest.mark.asyncio
async def test_view_image_rejects_non_image_bytes_with_raster_extension() -> None:
    session = scripted_sandbox_session(
        [{"method": "read", "result": io.BytesIO(b"not an image\n")}]
    )
    tool = ViewImageTool(session=session)

    output = await tool.run(ViewImageArgs(path="images/fake.png"))

    assert output == "image path `images/fake.png` is not a supported image file"
    session.assert_complete()


@pytest.mark.asyncio
async def test_view_image_ignores_mutated_mime_mapping_for_raster_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mimetypes, "guess_type", lambda _: ("image/svg+xml", None))
    session = scripted_sandbox_session(
        [{"method": "read", "result": io.BytesIO(b"not an image\n")}]
    )
    tool = ViewImageTool(session=session)

    output = await tool.run(ViewImageArgs(path="images/fake.png"))

    assert output == "image path `images/fake.png` is not a supported image file"
    session.assert_complete()


@pytest.mark.asyncio
async def test_view_image_accepts_raster_signature_without_image_extension() -> None:
    session = scripted_sandbox_session([{"method": "read", "result": io.BytesIO(_PNG_BYTES)}])
    tool = ViewImageTool(session=session)

    output = await tool.run(ViewImageArgs(path="images/payload.bin"))

    assert isinstance(output, ToolOutputImage)
    assert output.image_url is not None
    assert output.image_url.startswith("data:image/png;base64,")
    session.assert_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "svg_payload",
    [
        b"\xef\xbb\xbf" + _SVG_BODY,
        b"<!-- generated -->\n" + _SVG_BODY,
        b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "svg11.dtd">\n' + _SVG_BODY,
        _SVG_TEXT.encode("utf-16"),
    ],
    ids=["utf8-bom", "comment", "doctype", "utf16"],
)
async def test_view_image_preserves_svg_filename_compatibility(svg_payload: bytes) -> None:
    session = scripted_sandbox_session([{"method": "read", "result": io.BytesIO(svg_payload)}])
    tool = ViewImageTool(session=session)

    output = await tool.run(ViewImageArgs(path="images/vector.svg"))

    assert isinstance(output, ToolOutputImage)
    assert output.image_url is not None
    assert output.image_url.startswith("data:image/svg+xml;base64,")
    session.assert_complete()


@pytest.mark.asyncio
async def test_view_image_preserves_svgz_filename_compatibility() -> None:
    svgz_payload = gzip.compress(_SVG_BODY)
    session = scripted_sandbox_session([{"method": "read", "result": io.BytesIO(svgz_payload)}])
    tool = ViewImageTool(session=session)

    output = await tool.run(ViewImageArgs(path="images/vector.svgz"))

    assert isinstance(output, ToolOutputImage)
    assert output.image_url is not None
    assert output.image_url.startswith("data:image/svg+xml;base64,")
    session.assert_complete()
