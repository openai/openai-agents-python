"""
Islo-backed sandbox example: zero-trust egress with a per-sandbox gateway profile.

This is the marquee example for the islo extension. It shows the feature that
distinguishes islo from the other hosted backends in this repo: rule-level egress
policy attached to the sandbox at create time.

Two paths are demonstrated:

1. Bind to an existing gateway profile by name or id.
2. Provision a fresh profile inline; the client tears it down on session delete.

Path 1 is the fast path (sandbox creation on a steady-state profile is ~5s).
Path 2 is the convenient path; it has higher latency on first bind because the
gateway plane needs to propagate the new profile before the sandbox can attach.

Prerequisites:
    uv sync --extra islo
    export ISLO_API_KEY=ak_...   # https://islo.dev

Run the existing-profile demo (default):
    uv run python examples/sandbox/extensions/islo_gateway_runner.py

Run the inline-profile demo:
    uv run python examples/sandbox/extensions/islo_gateway_runner.py --inline
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys

from agents.sandbox import Manifest

try:
    from agents.extensions.sandbox.islo import (
        IsloGatewayProfile,
        IsloGatewayRule,
        IsloSandboxClient,
        IsloSandboxClientOptions,
    )
except Exception as exc:
    raise SystemExit(
        "islo sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra islo"
    ) from exc


def _build_inline_profile() -> IsloGatewayProfile:
    return IsloGatewayProfile(
        description="Demo agent egress for openai-agents-python islo example",
        default_action="deny",
        internet_enabled=False,
        rules=(
            IsloGatewayRule(
                host_pattern="api.openai.com",
                action="allow",
                rate_limit_rpm=120,
            ),
            IsloGatewayRule(
                host_pattern="*.github.com",
                methods=("GET",),
                action="allow",
            ),
            IsloGatewayRule(
                host_pattern="pypi.org",
                methods=("GET",),
                action="allow",
            ),
        ),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="islo sandbox demo")
    parser.add_argument(
        "--inline",
        action="store_true",
        help="provision a fresh inline gateway profile (slower; tests the create path)",
    )
    parser.add_argument(
        "--profile",
        default="default",
        help="existing gateway profile name or id when not running with --inline",
    )
    args = parser.parse_args()

    if not os.environ.get("ISLO_API_KEY"):
        print(
            "Set ISLO_API_KEY (export ISLO_API_KEY=ak_...) before running this example.",
            file=sys.stderr,
        )
        return 2

    gateway: object
    if args.inline:
        print("provisioning inline gateway profile (this may take a while on first bind)...")
        gateway = _build_inline_profile()
    else:
        print(f"using existing gateway profile: {args.profile}")
        gateway = args.profile

    client = IsloSandboxClient()
    options = IsloSandboxClientOptions(gateway_profile=gateway)

    print("creating sandbox...")
    session = await client.create(
        manifest=Manifest(),
        options=options,
    )
    inner = session._inner
    sandbox_name = getattr(inner.state, "sandbox_name", "<unknown>")
    print(f"sandbox: {sandbox_name}")

    try:
        await session.write("hello.txt", io.BytesIO(b"hi from islo\n"))
        echoed = await session.read("hello.txt")
        print(f"file round-trip: {echoed.read()!r}")

        result = await session.exec("uname", "-a")
        print(f"uname -a -> {result.stdout.decode().strip()!r} (exit={result.exit_code})")

        result = await session.exec("sh", "-lc", "echo $RANDOM-from-islo")
        print(f"shell command -> {result.stdout.decode().strip()!r} (exit={result.exit_code})")
    finally:
        print("deleting sandbox...")
        await client.delete(session)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
