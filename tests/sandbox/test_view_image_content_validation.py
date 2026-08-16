from __future__ import annotations

import base64
import io

import pytest

from agents.sandbox.capabilities.tools import ViewImageArgs, ViewImageTool
from agents.testing import scripted_sandbox_session
from agents.tool import ToolOutputImage

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a84QAAAAASUVORK5CYII="
)
_SVG_BODY = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'


@pytest.mark.asyncio
async def test_view_image_rejects_non_image_bytes_with_image_extension() -> None:
    session = scripted_sandbox_session(
        [{"method": "read", "result": io.BytesIO(b"not an image\n")}]
    )
    tool = ViewImageTool(session=session)

    output = await tool.run(ViewImageArgs(path="images/fake.png"))

    assert output == "image path `images/fake.png` is not a supported image file"
    session.assert_complete()


@pytest.mark.asyncio
async def test_view_image_accepts_image_signature_without_image_extension() -> None:
    session = scripted_sandbox_session(
        [{"method": "read", "result": io.BytesIO(_PNG_BYTES)}]
    )
    tool = ViewImageTool(session=session)

    output = await tool.run(ViewImageArgs(path="images/payload.bin"))

    assert isinstance(output, ToolOutputImage)
    assert output.image_url.startswith("data:image/png;base64,")
    session.assert_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "svg_payload",
    [
        b"\xef\xbb\xbf" + _SVG_BODY,
        b"<!-- generated -->\n" + _SVG_BODY,
        b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "svg11.dtd">\n' + _SVG_BODY,
        b"<!--" + (b"x" * 4096) + b"-->\n" + _SVG_BODY,
    ],
    ids=["utf8-bom", "comment", "doctype", "long-comment"],
)
async def test_view_image_accepts_svg_prolog_forms(svg_payload: bytes) -> None:
    session = scripted_sandbox_session(
        [{"method": "read", "result": io.BytesIO(svg_payload)}]
    )
    tool = ViewImageTool(session=session)

    output = await tool.run(ViewImageArgs(path="images/vector.svg"))

    assert isinstance(output, ToolOutputImage)
    assert output.image_url.startswith("data:image/svg+xml;base64,")
    session.assert_complete()
