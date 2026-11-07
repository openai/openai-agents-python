"""Search a project with native tools that take structured arguments.

Run from the matching SDK source archive with a preview wheel installed:
    python -m examples.codex.project_search /absolute/project "Find references to Invoice"

Set OPENAI_API_KEY through your normal credential injection. The local executor
requires Linux or macOS. For a remote executor, replace Environment.local with
Environment(url=..., headers=..., cwd=..., tools=...) and use a remote project path.

project_glob takes a glob pattern, such as **/*.py. project_grep takes a Rust
regular expression and an optional file glob. Both accept a relative path to
narrow the search and max_results (default 100, maximum 1000). Grep also accepts
context_lines (0 through 5). Results use paths relative to the configured root
and 1-based line numbers. They include matches, truncated, truncation_reasons
and errors. Each grep match includes line, before and after; context entries
contain line_number and line. An empty result means no match only when errors
is empty and truncated is false. If truncated is true, narrow the path or
pattern and search again; results are not a filesystem snapshot. Scan and
output limits can return fewer matches than max_results.

Search includes hidden files and does not apply .gitignore rules. It rejects
symlink components and cannot leave the configured working directory, even if
the Environment grants other writable roots. Grep searches UTF-8 regular files.
Remote executors need the matching native runtime with project search support.
"""

import asyncio
import sys
from pathlib import Path

from agents import Agent, Environment


async def main(project: Path, question: str) -> None:
    environment = Environment.local(cwd=project.absolute(), tools=["project_glob", "project_grep"])
    agent = Agent(
        instructions=(
            "Use project_glob to find files and project_grep to search their text. "
            "Cite returned paths and line numbers. If a search is truncated, "
            "narrow its path or pattern. Report search errors and incomplete coverage."
        ),
        model="gpt-5.5",
    )
    async with agent.session(environment=environment) as session:
        result = await (await session.submit(question)).get_result()
        print(result.final_output)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("Usage: project_search.py PROJECT QUESTION")
    asyncio.run(main(Path(sys.argv[1]), " ".join(sys.argv[2:])))
