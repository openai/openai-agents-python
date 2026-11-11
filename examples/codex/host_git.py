"""Push an application-selected commit through a narrowly scoped host tool."""

import argparse
import asyncio
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


def git(checkout: Path, *arguments: str) -> str:
    try:
        return subprocess.run(
            ["git", "-c", "http.followRedirects=false", "-C", str(checkout), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            text=True,
            errors="replace",
            timeout=90,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    # Raise outside the handler so subprocess output stays out of the tool error.
    raise RuntimeError("The selected host Git operation failed.")


@dataclass(frozen=True)
class PushTarget:
    checkout: Path
    remote_url: str
    commit: str
    branch: str

    def validate(self) -> None:
        if not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
            self.remote_url,
        ):
            raise ValueError("Select a credential-free HTTPS GitHub repository URL.")
        if not re.fullmatch(r"sdk-host-git-[A-Za-z0-9-]+", self.branch):
            raise ValueError("Select a fresh branch beginning with sdk-host-git-.")
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit):
            raise ValueError("Select a complete reviewed commit SHA.")
        fetch_url = git(self.checkout, "remote", "get-url", "--all", "origin")
        push_url = git(self.checkout, "remote", "get-url", "--push", "--all", "origin")
        if fetch_url != self.remote_url or push_url != self.remote_url:
            raise ValueError("The checkout's origin differs from the selected repository.")
        if git(self.checkout, "rev-parse", f"{self.commit}^{{commit}}") != self.commit:
            raise ValueError("The selected commit is unavailable in the checkout.")

    def remote_commit(self) -> str | None:
        ref = f"refs/heads/{self.branch}"
        row = git(self.checkout, "ls-remote", "--refs", "origin", ref)
        if not row:
            return None
        commit, _, name = row.partition("\t")
        if name != ref or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise RuntimeError("Git did not return the selected remote ref.")
        return commit

    def push(self) -> str:
        self.validate()
        current = self.remote_commit()
        if current == self.commit:
            return "The selected commit has already been verified on the selected branch."
        if current is not None:
            raise RuntimeError(
                "The selected destination branch already exists with another commit."
            )
        ref = f"refs/heads/{self.branch}"
        # An empty lease requires absence even if another writer races the preflight.
        git(
            self.checkout,
            "push",
            "--porcelain",
            "--no-follow-tags",
            "--recurse-submodules=no",
            f"--force-with-lease={ref}:",
            "origin",
            f"{self.commit}:{ref}",
        )
        if self.remote_commit() != self.commit:
            raise RuntimeError("The remote branch did not verify the selected commit.")
        return "The selected commit has been pushed and independently read back."


async def run(target: PushTarget) -> dict[str, str]:
    from agents import Agent, ToolContext, function_tool
    from agents.testing import ScriptedResponses, text, tool_call

    target.validate()

    @function_tool
    async def push_reviewed_commit(context: ToolContext[PushTarget]) -> str:
        """Push the one commit and new branch already selected by the application."""
        return await asyncio.to_thread(context.context.push)

    async with ScriptedResponses(
        [tool_call("push_reviewed_commit", {}, call_id="host-push")],
        [text("Done.")],
    ) as peer:
        agent = Agent[PushTarget, str](
            instructions="Use the application-selected host operation.",
            tools=[push_reviewed_commit],
            provider=peer.provider,
        )
        async with agent.session(context=target) as session:
            result = await (
                await session.submit("Push the reviewed commit to its selected test branch.")
            ).get_result()
            if result.final_output != "Done.":
                raise RuntimeError("The native turn did not settle normally.")
        output = peer.tool_output("host-push")
        if not isinstance(output, str) or ("verified" not in output and "read back" not in output):
            raise RuntimeError("The host tool did not confirm its remote result.")
    if target.remote_commit() != target.commit:
        raise RuntimeError("Final remote readback failed.")
    return {
        "repository": target.remote_url,
        "branch": target.branch,
        "commit": target.commit,
        "status": "verified",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--remote-url", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args()
    target = PushTarget(
        args.checkout.resolve(strict=True), args.remote_url, args.commit, args.branch
    )
    print(json.dumps(asyncio.run(run(target)), sort_keys=True))


if __name__ == "__main__":
    main()
