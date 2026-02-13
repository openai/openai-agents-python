import asyncio
import os
import subprocess

from agents import Agent, LocalShellCommandRequest, LocalShellTool, Runner, trace


def shell_executor(request: LocalShellCommandRequest) -> str:
    args = request.data.action

    try:
        completed = subprocess.run(
            args.command,
            cwd=args.working_directory or os.getcwd(),
            env={**os.environ, **args.env} if args.env else os.environ,
            capture_output=True,
            text=True,
            timeout=(args.timeout_ms / 1000) if args.timeout_ms else None,
        )
        return completed.stdout + completed.stderr

    except subprocess.TimeoutExpired:
        return "Command execution timed out"
    except Exception as e:
        return f"Error executing command: {str(e)}"


async def main():
    agent = Agent(
        name="Shell Assistant",
        instructions="You are a helpful assistant that can execute shell commands.",
        model="codex-mini-latest",  # Local shell tool requires a compatible model
        tools=[LocalShellTool(executor=shell_executor)],
    )

    with trace("Local shell example"):
        result = await Runner.run(
            agent,
            "List the files in the current directory and tell me how many there are.",
        )
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
