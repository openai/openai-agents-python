#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from urllib import error, parse, request

_RCLONE_RELEASES_API = "https://api.github.com/repos/rclone/rclone/releases"
_DEFAULT_COOLDOWN_DAYS = 7
_RUNTIME_PIN_PATH = Path("src/agents/extensions/sandbox/_rclone.py")
_DOCKER_PIN_PATH = Path("examples/sandbox/docker/Dockerfile.mount")
_PYTHON_PIN_BEGIN = "# BEGIN RCLONE RELEASE PIN"
_PYTHON_PIN_END = "# END RCLONE RELEASE PIN"
_DOCKER_PIN_BEGIN = "# BEGIN RCLONE RELEASE PIN"
_DOCKER_PIN_END = "# END RCLONE RELEASE PIN"
_RCLONE_ARCHES = ("386", "amd64", "arm", "arm-v6", "arm-v7", "arm64")
_DOCKER_ARCHES = ("amd64", "arm64")
_SHA256_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)$")


@dataclass(frozen=True)
class RclonePin:
    version: str
    sha256_by_arch: dict[str, str]


def _headers(url: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "openai-agents-python-rclone-pin-updater",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token and parse.urlparse(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_bytes(url: str) -> bytes:
    req = request.Request(url, headers=_headers(url))
    try:
        with request.urlopen(req, timeout=30) as response:
            return response.read()
    except error.HTTPError as exc:
        raise RuntimeError(f"failed to fetch {url}: HTTP {exc.code}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"failed to fetch {url}: {exc.reason}") from exc


def _fetch_json_object(url: str) -> dict[str, object]:
    payload = json.loads(_fetch_bytes(url))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected a JSON object from {url}")
    return cast(dict[str, object], payload)


def _fetch_json_array(url: str) -> list[dict[str, object]]:
    payload = json.loads(_fetch_bytes(url))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise RuntimeError(f"expected a JSON array of objects from {url}")
    return cast(list[dict[str, object]], payload)


def _normalized_version(value: str) -> str:
    version = value.removeprefix("v")
    if re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise ValueError(f"invalid rclone version: {value}")
    return version


def _release_url(version: str | None) -> str:
    if version is None:
        return f"{_RCLONE_RELEASES_API}?per_page=100"
    tag = parse.quote(f"v{_normalized_version(version)}", safe="")
    return f"{_RCLONE_RELEASES_API}/tags/{tag}"


def _release_version(release: dict[str, object]) -> str:
    tag_name = release.get("tag_name")
    if not isinstance(tag_name, str):
        raise RuntimeError("rclone release metadata is missing tag_name")
    return _normalized_version(tag_name)


def _release_published_at(release: dict[str, object]) -> datetime:
    value = release.get("published_at")
    if not isinstance(value, str):
        raise RuntimeError("rclone release metadata is missing published_at")
    try:
        published_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"rclone release has invalid published_at: {value}") from exc
    if published_at.tzinfo is None:
        raise RuntimeError(f"rclone release published_at has no timezone: {value}")
    return published_at.astimezone(timezone.utc)


def _asset_observed_at(release: dict[str, object], asset_name: str) -> datetime:
    asset = _asset(release, asset_name)
    timestamps: list[datetime] = []
    for field in ("created_at", "updated_at"):
        value = asset.get(field)
        if not isinstance(value, str):
            raise RuntimeError(f"rclone release asset {asset_name} is missing {field}")
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError(
                f"rclone release asset {asset_name} has invalid {field}: {value}"
            ) from exc
        if timestamp.tzinfo is None:
            raise RuntimeError(
                f"rclone release asset {asset_name} {field} has no timezone: {value}"
            )
        timestamps.append(timestamp.astimezone(timezone.utc))
    return max(timestamps)


def _required_asset_names(version: str) -> tuple[str, ...]:
    archives = tuple(f"rclone-v{version}-linux-{arch}.zip" for arch in _RCLONE_ARCHES)
    return ("SHA256SUMS", *archives)


def _validate_cooldown(
    subject: str,
    observed_at: datetime,
    *,
    cooldown_days: int,
    now: datetime,
) -> None:
    eligible_at = observed_at + timedelta(days=cooldown_days)
    if eligible_at > now:
        raise RuntimeError(
            f"{subject} is still in its {cooldown_days}-day cooldown "
            f"(eligible at {eligible_at.isoformat()})"
        )


def _validate_stable_release(
    release: dict[str, object],
    *,
    cooldown_days: int,
    now: datetime,
) -> None:
    version = _release_version(release)
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise RuntimeError(f"rclone v{version} is not a stable published release")
    _validate_cooldown(
        f"rclone v{version}",
        _release_published_at(release),
        cooldown_days=cooldown_days,
        now=now,
    )
    for asset_name in _required_asset_names(version):
        _validate_cooldown(
            f"rclone v{version} asset {asset_name}",
            _asset_observed_at(release, asset_name),
            cooldown_days=cooldown_days,
            now=now,
        )


def _latest_stable_release(
    releases: list[dict[str, object]],
    *,
    cooldown_days: int,
    now: datetime,
) -> dict[str, object]:
    eligible: list[tuple[datetime, dict[str, object]]] = []
    for release in releases:
        try:
            _validate_stable_release(release, cooldown_days=cooldown_days, now=now)
        except (RuntimeError, ValueError):
            continue
        eligible.append((_release_published_at(release), release))
    if not eligible:
        raise RuntimeError(
            f"no stable rclone release has completed the {cooldown_days}-day cooldown"
        )
    return max(eligible, key=lambda item: item[0])[1]


def _asset(release: dict[str, object], asset_name: str) -> dict[str, object]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("rclone release metadata is missing assets")
    for asset in assets:
        if isinstance(asset, dict) and asset.get("name") == asset_name:
            return cast(dict[str, object], asset)
    raise RuntimeError(f"rclone release is missing {asset_name}")


def _asset_url(release: dict[str, object], asset_name: str) -> str:
    url = _asset(release, asset_name).get("browser_download_url")
    if not isinstance(url, str):
        raise RuntimeError(f"rclone release asset {asset_name} is missing its download URL")
    return url


def _asset_sha256(release: dict[str, object], asset_name: str) -> str:
    digest = _asset(release, asset_name).get("digest")
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest) is None:
        raise RuntimeError(f"rclone release asset {asset_name} is missing its SHA256 digest")
    return digest.removeprefix("sha256:").lower()


def _validate_download_sha256(
    release: dict[str, object],
    asset_name: str,
    content: bytes,
) -> None:
    expected = _asset_sha256(release, asset_name)
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"downloaded rclone release asset {asset_name} does not match GitHub's digest"
        )


def _parse_sha256s(text: str, version: str) -> dict[str, str]:
    filenames = {f"rclone-v{version}-linux-{arch}.zip": arch for arch in _RCLONE_ARCHES}
    sha256_by_arch: dict[str, str] = {}
    for line in text.splitlines():
        match = _SHA256_LINE.fullmatch(line.strip())
        if match is None:
            continue
        digest, filename = match.groups()
        arch = filenames.get(filename)
        if arch is not None:
            sha256_by_arch[arch] = digest.lower()

    missing = [arch for arch in _RCLONE_ARCHES if arch not in sha256_by_arch]
    if missing:
        raise RuntimeError(
            f"rclone v{version} SHA256SUMS is missing Linux archives for: {', '.join(missing)}"
        )
    return sha256_by_arch


def _validate_asset_sha256s(
    release: dict[str, object],
    version: str,
    sha256_by_arch: dict[str, str],
) -> None:
    for arch in _RCLONE_ARCHES:
        asset_name = f"rclone-v{version}-linux-{arch}.zip"
        asset_sha256 = _asset_sha256(release, asset_name)
        if asset_sha256 != sha256_by_arch[arch]:
            raise RuntimeError(
                f"rclone v{version} SHA256SUMS does not match GitHub's digest for {asset_name}"
            )


def fetch_pin(
    version: str | None = None,
    *,
    cooldown_days: int = _DEFAULT_COOLDOWN_DAYS,
    now: datetime | None = None,
) -> RclonePin:
    if cooldown_days < 0:
        raise ValueError("cooldown days must be zero or greater")
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("current time must include a timezone")
    current_time = current_time.astimezone(timezone.utc)

    if version is None:
        releases = _fetch_json_array(_release_url(None))
        release = _latest_stable_release(
            releases,
            cooldown_days=cooldown_days,
            now=current_time,
        )
    else:
        release = _fetch_json_object(_release_url(version))
        _validate_stable_release(
            release,
            cooldown_days=cooldown_days,
            now=current_time,
        )
    resolved_version = _release_version(release)
    if version is not None and resolved_version != _normalized_version(version):
        raise RuntimeError(
            f"requested rclone v{_normalized_version(version)}, got v{resolved_version}"
        )
    checksums_url = _asset_url(release, "SHA256SUMS")
    checksums_content = _fetch_bytes(checksums_url)
    _validate_download_sha256(release, "SHA256SUMS", checksums_content)
    checksums = checksums_content.decode("utf-8")
    sha256_by_arch = _parse_sha256s(checksums, resolved_version)
    _validate_asset_sha256s(release, resolved_version, sha256_by_arch)
    return RclonePin(
        version=resolved_version,
        sha256_by_arch=sha256_by_arch,
    )


def _python_pin_block(pin: RclonePin) -> str:
    lines = [
        _PYTHON_PIN_BEGIN,
        f'_RCLONE_VERSION = "{pin.version}"',
        "_RCLONE_SHA256_BY_ARCH = {",
    ]
    for arch in _RCLONE_ARCHES:
        lines.append(f'    "{arch}": "{pin.sha256_by_arch[arch]}",')
    lines.extend(["}", _PYTHON_PIN_END])
    return "\n".join(lines)


def _docker_pin_block(pin: RclonePin) -> str:
    lines = [_DOCKER_PIN_BEGIN, f"ARG RCLONE_VERSION={pin.version}"]
    for arch in _DOCKER_ARCHES:
        variable_arch = arch.upper().replace("-", "_")
        lines.append(f"ARG RCLONE_SHA256_LINUX_{variable_arch}={pin.sha256_by_arch[arch]}")
    lines.append(_DOCKER_PIN_END)
    return "\n".join(lines)


def _replace_marked_block(text: str, begin: str, end: str, replacement: str) -> str:
    if text.count(begin) != 1 or text.count(end) != 1:
        raise RuntimeError(f"expected exactly one pin block delimited by {begin!r} and {end!r}")
    start = text.index(begin)
    finish = text.index(end, start) + len(end)
    return f"{text[:start]}{replacement}{text[finish:]}"


def apply_pin(repo_root: Path, pin: RclonePin, *, check: bool) -> list[Path]:
    updates = (
        (
            repo_root / _RUNTIME_PIN_PATH,
            _PYTHON_PIN_BEGIN,
            _PYTHON_PIN_END,
            _python_pin_block(pin),
        ),
        (
            repo_root / _DOCKER_PIN_PATH,
            _DOCKER_PIN_BEGIN,
            _DOCKER_PIN_END,
            _docker_pin_block(pin),
        ),
    )
    changed: list[Path] = []
    for path, begin, end, replacement in updates:
        current = path.read_text()
        updated = _replace_marked_block(current, begin, end, replacement)
        if updated == current:
            continue
        changed.append(path)
        if not check:
            path.write_text(updated)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update the verified rclone release pinned by sandbox installers."
    )
    parser.add_argument(
        "--version",
        help="rclone version to pin, with or without a leading v (default: latest stable)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report stale pin blocks without modifying files",
    )
    parser.add_argument(
        "--cooldown-days",
        type=int,
        default=_DEFAULT_COOLDOWN_DAYS,
        help=(
            "minimum age of a stable release before it is eligible "
            f"(default: {_DEFAULT_COOLDOWN_DAYS})"
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    try:
        pin = fetch_pin(args.version, cooldown_days=args.cooldown_days)
        changed = apply_pin(args.repo_root.resolve(), pin, check=args.check)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not changed:
        print(f"rclone v{pin.version} pin is current")
        return 0

    relative_paths = [str(path.relative_to(args.repo_root.resolve())) for path in changed]
    if args.check:
        print(
            f"::error title=Stale rclone pin::rclone v{pin.version} differs in "
            f"{', '.join(relative_paths)}",
            file=sys.stderr,
        )
        print(
            f"Run: python .github/scripts/update_rclone_pin.py --version {pin.version}",
            file=sys.stderr,
        )
        return 1

    print(f"updated rclone v{pin.version} pin in {', '.join(relative_paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
