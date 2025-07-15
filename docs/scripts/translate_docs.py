# ruff: noqa
import os
import sys
import argparse
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor

# import logging
# logging.basicConfig(level=logging.INFO)
# logging.getLogger("openai").setLevel(logging.DEBUG)

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "o3")

ENABLE_CODE_SNIPPET_EXCLUSION = True
# gpt-4.5 needed this for better quality
ENABLE_SMALL_CHUNK_TRANSLATION = False

SEARCH_EXCLUSION = """---
search:
  exclude: true
---
"""

# Define the source and target directories
source_dir = "docs"
languages = {
    "ja": "Japanese",
    # Add more languages here, e.g., "fr": "French"
}

# Initialize OpenAI client
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Define dictionaries for translation control
do_not_translate = [
    "OpenAI",
    "Agents SDK",
    "Hello World",
    "Model context protocol",
    "MCP",
    "structured outputs",
    "Chain-of-Thought",
    "Chat Completions",
    "Computer-Using Agent",
    "Code Interpreter",
    "Function Calling",
    "LLM",
    "Operator",
    "Playground",
    "Realtime API",
    "Sora",
    # Add more terms here
]

# ... (other mapping definitions unchanged) ...

def built_instructions(target_language: str, lang_code: str) -> str:
    # (function body unchanged)
    ...

# Function to translate and save files
def translate_file(file_path: str, target_path: str, lang_code: str) -> None:
    print(f"Translating {file_path} into a different language: {lang_code}")
    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    # Split content into lines
    lines: list[str] = content.splitlines()
    chunks: list[str] = []
    current_chunk: list[str] = []

    # Split content into chunks of up to 120 lines, ensuring splits occur before section titles
    in_code_block = False
    code_blocks: list[str] = []
    code_block_chunks: list[str] = []
    for line in lines:
        # (chunking logic unchanged)
        ...
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    # Translate each chunk separately and combine results
    translated_content: list[str] = []
    for chunk in chunks:
        instructions = built_instructions(languages[lang_code], lang_code)

        # Plain dict-based system+user messages
        messages: list[dict[str, str]] = [
            {"role": "system", "content": instructions},
            {"role": "user",   "content": chunk},
        ]

        if OPENAI_MODEL.startswith("o"):
            # type: ignore[arg-type] for messages mismatch with overload
            response = openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,  # type: ignore[arg-type]
            )
        else:
            response = openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,  # type: ignore[arg-type]
                temperature=0.0,
            )

        # Extract and append the text (fallback to empty string if None)
        text = response.choices[0].message.content or ""
        translated_content.append(text)

    # Combine all chunks into one markdown string
    translated_text = "\n".join(translated_content)

    for idx, code_block in enumerate(code_blocks):
        translated_text = translated_text.replace(f"CODE_BLOCK_{idx:02}", code_block)

    # FIXME: enable mkdocs search plugin to seamlessly work with i18n plugin
    translated_text = SEARCH_EXCLUSION + translated_text
    # Save the combined translated content
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(translated_text)


def translate_single_source_file(file_path: str) -> None:
    relative_path = os.path.relpath(file_path, source_dir)
    if "ref/" in relative_path or not file_path.endswith(".md"):
        return

    for lang_code in languages:
        target_dir = os.path.join(source_dir, lang_code)
        target_path = os.path.join(target_dir, relative_path)

        # Ensure the target directory exists
        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        # Translate and save the file
        translate_file(file_path, target_path, lang_code)


def main():
    parser = argparse.ArgumentParser(description="Translate documentation files")
    parser.add_argument(
        "--file", type=str, help="Specific file to translate (relative to docs directory)"
    )
    args = parser.parse_args()

    if args.file:
        # Translate a single file
        # Handle both "foo.md" and "docs/foo.md" formats
        if args.file.startswith("docs/"):
            # Remove "docs/" prefix if present
            relative_file = args.file[5:]
        else:
            relative_file = args.file

        file_path = os.path.join(source_dir, relative_file)
        if os.path.exists(file_path):
            translate_single_source_file(file_path)
            print(f"Translation completed for {relative_file}")
        else:
            print(f"Error: File {file_path} does not exist")
            sys.exit(1)
    else:
        # Traverse the source directory (original behavior)
        for root, _, file_names in os.walk(source_dir):
            # Skip the target directories
            if any(lang in root for lang in languages):
                continue
            # Increasing this will make the translation faster; you can decide considering the model's capacity
            concurrency = 6
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = []
                for file_name in file_names:
                    filepath = os.path.join(root, file_name)
                    futures.append(executor.submit(translate_single_source_file, filepath))
                    if len(futures) >= concurrency:
                        for future in futures:
                            future.result()
                        futures.clear()

        print("Translation completed.")


if __name__ == "__main__":
    # translate_single_source_file("docs/index.md")
    main()
