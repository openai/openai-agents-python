# ruff: noqa
import os
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor


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
    "Model Context Protocol",
    "structured outputs",
    # Add more terms here
]

eng_to_non_eng_mapping = {
    "ja": {
        "agents": "エージェント",
        "computer use": "コンピュータ操作",
        "OAI hosted tools": "OpenAI がホストするツール",
        "well formed data": "適切な形式のデータ",
        "guardrail": "ガードレール",
        "handoffs": "ハンドオフ",
        "function tools": "関数ツール",
        "tracing": "トレーシング",
        "code examples": "コード例",
        "vector store": "ベクトルストア",
        # Add more Japanese mappings here
    },
    # Add more languages here
}
eng_to_non_eng_instructions = {
    "ja": {
        "The term 'result' in the Runner guide context must be translated like 'execution results'",
        "The term 'raw' in 'raw response events' must be kept as is",
        "The term 'examples' must be code examples when the page mentions the code examples in the repo, it can be translated as either 'code exmaples' or 'sample code'.",
        "The term 'primitives' can be translated as basic components or building blocks.",
        "When the terms 'instructions' and 'tools' are mentioned as API parameter names, they must be kept as is.",
        # Add more Japanese mappings here
    },
    # Add more languages here
}


def built_instructions(target_language: str, lang_code: str) -> str:
    do_not_translate_terms = "\n".join(do_not_translate)
    specific_terms = "\n".join(
        [f"{k} -> {v}" for k, v in eng_to_non_eng_mapping.get(lang_code, {}).items()]
    )
    specific_instructions = "\n".join(eng_to_non_eng_instructions.get(lang_code, {}))
    return f"""You are a professional translator with extensive experience in translating technical documents.
You are assigned to translate markdown text written in English into {target_language}.
The tone and voice must be concise, consistent, and most importantly professional.
You must return only the generated markdown text. Don't include any additional comments.
When you're unable to complete full translation, return an error message indicating the reason instead of returning partial results.

# Do not translate
{do_not_translate_terms}

# Specific term mappings
When you convert these terms, do not append whitespaces before/after the terms.
{specific_terms}
{specific_instructions}

# Other Rules
- When translating into Japanese, ensure there are spaces before and after alphanumeric terms and markdown special characters like italic and bold.
- When translating very uncommon technical terms, include both the translated term and the original term in parentheses. That said, the section titles should be as simple as possible.
- You must skip translating any parts of code snippets and code comments
- "./assets/*" needs to be converted to "../assets/*"; markdown files like ./tracing.md can be kept as is.
"""


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
    for line in lines:
        if len(current_chunk) >= 120 and line.startswith("#"):
            chunks.append("\n".join(current_chunk))
            current_chunk = []
        current_chunk.append(line)
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    # Translate each chunk separately and combine results
    translated_content: list[str] = []
    for chunk in chunks:
        response = openai_client.responses.create(
            model="gpt-4.5-preview",
            temperature=0.0,
            instructions=built_instructions(languages[lang_code], lang_code),
            input=chunk,
        )
        translated_content.append(response.output_text)

    # Save the combined translated content
    with open(target_path, "w", encoding="utf-8") as f:
        f.write("\n".join(translated_content))


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
    # Traverse the source directory
    for root, _, file_names in os.walk(source_dir):
        # Skip the target directories
        if any(lang in root for lang in languages):
            continue
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [
                executor.submit(
                    translate_single_source_file,
                    os.path.join(root, file_name),
                )
                for file_name in file_names
            ]
            for future in futures:
                future.result()

    print("Translation completed.")


if __name__ == "__main__":
    # translate_single_source_file("docs/tools.md")
    main()
