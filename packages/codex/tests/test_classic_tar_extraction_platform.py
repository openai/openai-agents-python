"""Unsupported local TAR extraction fails before touching the destination."""

import io
import sys
import tarfile
from pathlib import Path

import pytest

from agents.classic.sandbox.util.tar_utils import safe_extract_tarfile


def test_tar_extraction_rejects_windows_before_creating_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    with tarfile.open(fileobj=io.BytesIO(bytes(10240))) as archive:
        monkeypatch.setattr(sys, "platform", "win32")
        with pytest.raises(NotImplementedError, match="requires Unix directory descriptors"):
            safe_extract_tarfile(archive, root=root)

    assert not root.exists()
