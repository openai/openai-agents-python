"""Run bounded document tools over decoded text selected by the application.

See selected_documents.md for setup, permissions, and file limits.
Run `python -m examples.codex.selected_documents` from the repository root.
It requires OPENAI_API_KEY, makes live requests, and can incur charges.
"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from agents import Agent, NativeProvider
from examples.codex.document_workspace import PAGE_CHARS, SelectedDocuments


def document_agent(documents: SelectedDocuments, provider: NativeProvider) -> Agent[None, str]:
    """Borrow a provider; the caller closes it after all document Sessions."""
    return Agent(
        instructions=(
            "Use only the document aliases selected by the application. Read or search "
            "only the pages needed. Follow next_offset with the same revision; restart "
            "a read if its revision is stale. Before editing, read the writable document "
            "and use its current revision. Report a saved edit only after the tool "
            "confirms it was saved and reopened."
        ),
        tools=documents.tools(),
        provider=provider,
    )


async def main() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "decoded.txt"
        # A real application supplies decoded text, such as text extracted from a PDF.
        path.write_bytes(("Status: draft\n" + "🌿 Field note.\n" * PAGE_CHARS).encode("utf-8"))
        documents = SelectedDocuments(
            root=root, selected={"notes": "decoded.txt"}, writable=["notes"]
        )
        async with NativeProvider() as provider:
            result = await document_agent(documents, provider).run(
                "In notes, replace the unique Status: draft with Status: reviewed. "
                "Keep all other text unchanged."
            )
            print(result.final_output)
        print(path.read_text(encoding="utf-8").splitlines()[0])


if __name__ == "__main__":
    asyncio.run(main())
