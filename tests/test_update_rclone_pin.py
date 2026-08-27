from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest


def _load_updater() -> ModuleType:
    path = Path(__file__).parents[1] / ".github/scripts/update_rclone_pin.py"
    spec = importlib.util.spec_from_file_location("update_rclone_pin", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


updater = _load_updater()


def _checksums(version: str) -> str:
    return "\n".join(
        f"{index:064x}  rclone-v{version}-linux-{arch}.zip"
        for index, arch in enumerate(updater._RCLONE_ARCHES, start=1)
    )


def _release(
    version: str,
    published_at: str,
    *,
    asset_observed_at: str | None = None,
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, object]:
    asset_timestamp = asset_observed_at or published_at
    return {
        "tag_name": f"v{version}",
        "published_at": published_at,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {
                "name": asset_name,
                "created_at": asset_timestamp,
                "updated_at": asset_timestamp,
            }
            for asset_name in updater._required_asset_names(version)
        ],
    }


def test_latest_stable_release_respects_default_cooldown() -> None:
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    releases = [
        _release("1.75.0", "2026-07-20T00:00:00Z"),
        _release("1.74.5", "2026-07-10T00:00:00Z", prerelease=True),
        _release("1.74.4", "2026-07-08T00:00:00Z"),
        _release("1.74.3", "2026-07-01T00:00:00Z"),
    ]

    selected = updater._latest_stable_release(
        releases,
        cooldown_days=updater._DEFAULT_COOLDOWN_DAYS,
        now=now,
    )

    assert updater._release_version(selected) == "1.74.4"


def test_latest_stable_release_skips_release_with_fresh_assets() -> None:
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    releases = [
        _release(
            "1.75.0",
            "2026-07-01T00:00:00Z",
            asset_observed_at="2026-07-20T00:00:00Z",
        ),
        _release("1.74.4", "2026-07-08T00:00:00Z"),
    ]

    selected = updater._latest_stable_release(
        releases,
        cooldown_days=updater._DEFAULT_COOLDOWN_DAYS,
        now=now,
    )

    assert updater._release_version(selected) == "1.74.4"


def test_stable_release_cooldown_is_customizable() -> None:
    release = _release("1.75.0", "2026-07-20T00:00:00Z")
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with pytest.raises(RuntimeError, match="7-day cooldown"):
        updater._validate_stable_release(release, cooldown_days=7, now=now)

    updater._validate_stable_release(release, cooldown_days=2, now=now)


@pytest.mark.parametrize(
    "asset_name",
    updater._required_asset_names("1.74.4"),
)
def test_stable_release_cooldown_covers_every_required_asset(asset_name: str) -> None:
    release = _release("1.74.4", "2026-07-01T00:00:00Z")
    asset = updater._asset(release, asset_name)
    asset["created_at"] = "2026-07-20T00:00:00Z"
    asset["updated_at"] = "2026-07-20T00:00:00Z"

    with pytest.raises(RuntimeError, match=re.escape(f"asset {asset_name}")):
        updater._validate_stable_release(
            release,
            cooldown_days=7,
            now=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )


def test_stable_release_cooldown_uses_latest_asset_timestamp() -> None:
    release = _release("1.74.4", "2026-07-01T00:00:00Z")
    asset = updater._asset(release, "SHA256SUMS")
    asset["updated_at"] = "2026-07-20T00:00:00Z"

    with pytest.raises(RuntimeError, match="asset SHA256SUMS"):
        updater._validate_stable_release(
            release,
            cooldown_days=7,
            now=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    ("draft", "prerelease"),
    [(True, False), (False, True)],
)
def test_release_selection_rejects_drafts_and_prereleases(
    draft: bool,
    prerelease: bool,
) -> None:
    release = _release(
        "1.74.4",
        "2026-07-01T00:00:00Z",
        draft=draft,
        prerelease=prerelease,
    )

    with pytest.raises(RuntimeError, match="not a stable published release"):
        updater._validate_stable_release(
            release,
            cooldown_days=0,
            now=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )


def test_parse_sha256s_requires_every_runtime_architecture() -> None:
    version = "1.2.3"

    parsed = updater._parse_sha256s(_checksums(version), version)

    assert list(parsed) == list(updater._RCLONE_ARCHES)
    assert parsed["amd64"] == f"{2:064x}"


def test_headers_do_not_send_github_token_to_release_asset_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret")

    assert (
        updater._headers("https://api.github.com/repos/rclone/rclone/releases/latest")[
            "Authorization"
        ]
        == "Bearer secret"
    )
    assert "Authorization" not in updater._headers(
        "https://github.com/rclone/rclone/releases/download/v1.2.3/SHA256SUMS"
    )


def test_parse_sha256s_rejects_incomplete_release() -> None:
    with pytest.raises(RuntimeError, match="arm64"):
        updater._parse_sha256s(
            "1" * 64 + "  rclone-v1.2.3-linux-amd64.zip\n",
            "1.2.3",
        )


def test_validate_asset_sha256s_requires_github_asset_match() -> None:
    version = "1.2.3"
    sha256_by_arch = updater._parse_sha256s(_checksums(version), version)
    release = {
        "assets": [
            {
                "name": f"rclone-v{version}-linux-{arch}.zip",
                "digest": f"sha256:{sha256_by_arch[arch]}",
            }
            for arch in updater._RCLONE_ARCHES
        ]
    }

    updater._validate_asset_sha256s(release, version, sha256_by_arch)
    release["assets"][0]["digest"] = f"sha256:{'f' * 64}"

    with pytest.raises(RuntimeError, match="does not match GitHub"):
        updater._validate_asset_sha256s(release, version, sha256_by_arch)


def test_validate_download_sha256_requires_github_asset_match() -> None:
    content = b"rclone checksums"
    release = {
        "assets": [
            {
                "name": "SHA256SUMS",
                "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
            }
        ]
    }

    updater._validate_download_sha256(release, "SHA256SUMS", content)

    with pytest.raises(RuntimeError, match="does not match GitHub"):
        updater._validate_download_sha256(release, "SHA256SUMS", b"changed")


def test_apply_pin_updates_and_checks_both_consumers(tmp_path: Path) -> None:
    runtime_path = tmp_path / updater._RUNTIME_PIN_PATH
    docker_path = tmp_path / updater._DOCKER_PIN_PATH
    runtime_path.parent.mkdir(parents=True)
    docker_path.parent.mkdir(parents=True)
    runtime_path.write_text(
        f"before\n{updater._PYTHON_PIN_BEGIN}\nold\n{updater._PYTHON_PIN_END}\nafter\n"
    )
    docker_path.write_text(
        f"before\n{updater._DOCKER_PIN_BEGIN}\nold\n{updater._DOCKER_PIN_END}\nafter\n"
    )
    pin = updater.RclonePin(
        version="1.2.3",
        sha256_by_arch=updater._parse_sha256s(_checksums("1.2.3"), "1.2.3"),
    )

    assert updater.apply_pin(tmp_path, pin, check=True) == [runtime_path, docker_path]
    assert "old" in runtime_path.read_text()

    assert updater.apply_pin(tmp_path, pin, check=False) == [runtime_path, docker_path]
    assert updater.apply_pin(tmp_path, pin, check=True) == []
    assert '_RCLONE_VERSION = "1.2.3"' in runtime_path.read_text()
    assert "ARG RCLONE_VERSION=1.2.3" in docker_path.read_text()
