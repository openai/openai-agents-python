# ruff: noqa
import os
import sys
import argparse
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

try:
    import yaml  # type: ignore
except Exception:
    yaml = None  # PyYAML may not be available in some environments

# import logging
# logging.basicConfig(level=logging.INFO)
# logging.getLogger("openai").setLevel(logging.DEBUG)

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")

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
    "ko": "Korean",
    # Add more languages here, e.g., "fr": "French"
}

# Comma-separated list to restrict which languages to translate (e.g., "ko" or "ja,ko")
ONLY_LANGS = [
    s.strip()
    for s in (os.environ.get("ONLY_LANG") or os.environ.get("LANGS") or "").split(",")
    if s.strip()
]

# Initialize OpenAI client
api_key = os.getenv("PROD_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=api_key)

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
        "deep research": "ディープリサーチ",
        "category": "カテゴリー",
        "user": "ユーザー",
        "parameter": "パラメーター",
        "processor": "プロセッサー",
        "server": "サーバー",
        "web search": "Web 検索",
        "file search": "ファイル検索",
        "streaming": "ストリーミング",
        "system prompt": "システムプロンプト",
        "Python first": "Python ファースト",
        # Add more Japanese mappings here
    },
    "ko": {
        "agents": "에이전트",
        "computer use": "컴퓨터 사용",
        "OAI hosted tools": "OpenAI 호스트하는 도구",
        "well formed data": "적절한 형식의 데이터",
        "guardrail": "가드레일",
        "orchestrating multiple agents": "멀티 에이전트 오케스트레이션",
        "handoffs": "핸드오프",
        "function tools": "함수 도구",
        "function calling": "함수 호출",
        "tracing": "트레이싱",
        "code examples": "코드 예제",
        "vector store": "벡터 스토어",
        "deep research": "딥 리서치",
        "category": "카테고리",
        "user": "사용자",
        "parameter": "매개변수",
        "processor": "프로세서",
        "server": "서버",
        "web search": "웹 검색",
        "file search": "파일 검색",
        "streaming": "스트리밍",
        "system prompt": "시스템 프롬프트",
        "interruption": "인터럽션(중단 처리)",
        "TypeScript-first": "TypeScript 우선",
        "Human in the loop": "휴먼인더루프 (HITL)",
        "Hosted tool": "호스티드 툴",
        "Hosted MCP server tools": "호스티드 MCP 서버 도구",
        "raw": "원문",
        "Realtime Agents": "실시간 에이전트",
        "Build your first agent in minutes.": "단 몇 분 만에 첫 에이전트를 만들 수 있습니다",
        "Let's build": "시작하기",
    },
    # Add more languages here
}
eng_to_non_eng_instructions = {
    "common": [
        "* The term 'examples' must be code examples when the page mentions the code examples in the repo, it can be translated as either 'code examples' or 'sample code'.",
        "* The term 'primitives' can be translated as basic components.",
        "* When the terms 'instructions' and 'tools' are mentioned as API parameter names, they must be kept as is.",
        "* The terms 'temperature', 'top_p', 'max_tokens', 'presence_penalty', 'frequency_penalty' as parameter names must be kept as is.",
        "* Keep the original structure like `* **The thing**: foo`; this needs to be translated as `* **(translation)**: (translation)`",
    ],
    "ja": [
        "* The term 'result' in the Runner guide context must be translated like 'execution results'",
        "* The term 'raw' in 'raw response events' must be kept as is",
        "* You must consistently use polite wording such as です/ます rather than である/なのだ.",
        # Add more Japanese mappings here
    ],
    "ko": [
        "* 공손하고 중립적인 문체(합니다/입니다체)를 일관되게 사용하세요.",
        "* 개발자 문서이므로 자연스러운 의역을 허용하되 정확성을 유지하세요.",
        "* 'instructions', 'tools' 같은 API 매개변수와 temperature, top_p, max_tokens, presence_penalty, frequency_penalty 등은 영문 그대로 유지하세요.",
        "* 문장이 아닌 불릿 항목 끝에는 마침표를 찍지 마세요.",
    ],
    # Add more languages here
}


def _extract_sidebar_translations(lang_code: str) -> Dict[str, Dict[str, Optional[str]]]:
    """Extract mapping of doc file paths to labels/translations from mkdocs.yml.

    Returns a map: { path: { "label": str, "translation": str|None } }
    """
    sidebar_map: Dict[str, Dict[str, Optional[str]]] = {}
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    mkdocs_path = os.path.join(repo_root, "mkdocs.yml")
    if yaml is None:
        return sidebar_map
    try:
        with open(mkdocs_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return sidebar_map

    try:
        languages_block = []
        for plugin in data.get("plugins", []):
            if isinstance(plugin, dict) and "i18n" in plugin:
                languages_block = plugin["i18n"].get("languages", [])
                break
        if not languages_block:
            return sidebar_map

        nav_by_locale: Dict[str, Any] = {}
        for lang in languages_block:
            locale = lang.get("locale")
            nav_by_locale[locale] = lang.get("nav")

        en_nav = nav_by_locale.get("en")
        tgt_nav = nav_by_locale.get(lang_code)

        def collect(nav: Any) -> Dict[str, str]:
            result: Dict[str, str] = {}
            if not isinstance(nav, list):
                return result
            for item in nav:
                if isinstance(item, dict):
                    for label, value in item.items():
                        if isinstance(value, str):
                            result[value] = str(label)
                        else:
                            result.update(collect(value))
                elif isinstance(item, str):
                    continue
            return result

        en_map = collect(en_nav) if en_nav else {}
        tgt_map = collect(tgt_nav) if tgt_nav else {}
        for path_key, en_label in en_map.items():
            sidebar_map[path_key] = {
                "label": en_label,
                "translation": tgt_map.get(path_key),
            }
    except Exception:
        return {}
    return sidebar_map


def built_instructions(
    target_language: str,
    lang_code: str,
    sidebar_map: Optional[Dict[str, Dict[str, Optional[str]]]] = None,
) -> str:
    do_not_translate_terms = "\n".join(do_not_translate)
    specific_terms = "\n".join(
        [f"* {k} -> {v}" for k, v in eng_to_non_eng_mapping.get(lang_code, {}).items()]
    )
    specific_instructions = "\n".join(
        eng_to_non_eng_instructions.get("common", [])
        + eng_to_non_eng_instructions.get(lang_code, [])
    )
    sidebar_labels_block = ""
    if sidebar_map:
        label_lines: List[str] = []
        for link, entry in sidebar_map.items():
            if entry.get("translation"):
                label_lines.append(
                    f"- {link}: {entry['translation']} (sidebar translation)"
                )
            elif entry.get("label"):
                label_lines.append(f"- {link}: {entry['label']} (sidebar label)")
        if label_lines:
            sidebar_labels_block = (
                "\n\n#########################\n##  PAGE TITLES        ##\n#########################\n"
                "When you see links to another page, consistently use the following labels:\n"
                + "\n".join(label_lines)
                + "\n\nAlways use these canonical translations for page titles and references."
            )
    return f"""You are an expert technical translator.

Your task: translate the markdown passed as a user input from English into {target_language}.
The inputs are the official OpenAI Agents SDK framework documentation, and your translation outputs'll be used for serving the official {target_language} version of them. Thus, accuracy, clarity, and fidelity to the original are critical.

############################
##  OUTPUT REQUIREMENTS  ##
############################
You must return **only** the translated markdown. Do not include any commentary, metadata, or explanations. The original markdown structure must be strictly preserved.

#########################
##  GENERAL RULES      ##
#########################
- Be professional and polite.
- Keep the tone **natural** and concise.
- Do not omit any content. If a segment should stay in English, copy it verbatim.
- Do not change the markdown data structure, including the indentations.
- Section titles starting with # or ## must be a noun form rather than a sentence.
- Section titles must be translated except for the Do-Not-Translate list.
- Keep all placeholders such as `CODE_BLOCK_*` and `CODE_LINE_PREFIX` unchanged.
- Convert asset paths: `./assets/…` → `../assets/…`.  
  *Example:* `![img](./assets/pic.png)` → `![img](../assets/pic.png)`
- Treat the **Do‑Not‑Translate list** and **Term‑Specific list** as case‑insensitive; preserve the original casing you see.
- Skip translation for:
  - Inline code surrounded by single back‑ticks ( `like_this` ).
  - Fenced code blocks delimited by ``` or ~~~, including all comments inside them.
  - Link URLs inside `[label](URL)` – translate the label, never the URL.
- When translating Markdown tables, preserve the exact table structure, including all delimiters (|), header separators (---), and row/column counts. Only translate the cell contents. Do not add, remove, or reorder columns or rows.
{sidebar_labels_block}

#########################
##  LANGUAGE‑SPECIFIC  ##
#########################
*(applies only when {target_language} = Japanese)*  
- Insert a half‑width space before and after all alphanumeric terms.  
- Add a half‑width space just outside markdown emphasis markers: ` **太字** ` (good) vs `** 太字 **` (bad).
*(applies only when {target_language} = Korean)*  
- Do not alter spaces around code/identifiers; keep them as in the original.  
- Do not add stray spaces around markdown emphasis: `**굵게**` (good) vs `** 굵게 **` (bad).

#########################
##  DO NOT TRANSLATE   ##
#########################
When replacing the following terms, do not have extra spaces before/after them:
{do_not_translate_terms}

#########################
##  TERM‑SPECIFIC      ##
#########################
Translate these terms exactly as provided (no extra spaces):  
{specific_terms}

#########################
##  EXTRA GUIDELINES   ##
#########################
{specific_instructions}

#########################
##  IF UNSURE          ##
#########################
If you are uncertain about a term, leave the original English term in parentheses after your translation.

#########################
##  WORKFLOW           ##
#########################

Follow the following workflow to translate the given markdown text data:

1. Read the input markdown text given by the user.
2. Translate the markdown file into {target_language}, carefully following the requirements above.
3. Self-review your translation to ensure high quality, focusing on naturalness, accuracy, and consistency while avoiding unnecessary changes or spacing.
4. If improvements are necessary, refine the content without changing the original meaning.
5. Continue improving the translation until you are fully satisfied with the result.
6. Once the final output is ready, return **only** the translated markdown text. No extra commentary.
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
    in_code_block = False
    code_blocks: list[str] = []
    code_block_chunks: list[str] = []
    for line in lines:
        # Treat single-line import statements as code blocks to avoid accidental translation
        if (
            ENABLE_CODE_SNIPPET_EXCLUSION is True
            and (in_code_block is False)
            and line.startswith("import ")
        ):
            code_blocks.append(line)
            current_chunk.append(f"CODE_BLOCK_{(len(code_blocks) - 1):02}")
            continue
        if (
            ENABLE_SMALL_CHUNK_TRANSLATION is True
            and len(current_chunk) >= 120  # required for gpt-4.5
            and not in_code_block
            and line.startswith("#")
        ):
            chunks.append("\n".join(current_chunk))
            current_chunk = []
        if ENABLE_CODE_SNIPPET_EXCLUSION is True and line.strip().startswith("```"):
            code_block_chunks.append(line)
            if in_code_block is True:
                code_blocks.append("\n".join(code_block_chunks))
                current_chunk.append(f"CODE_BLOCK_{(len(code_blocks) - 1):02}")
                code_block_chunks.clear()
            in_code_block = not in_code_block
            continue
        if in_code_block is True:
            code_block_chunks.append(line)
        else:
            current_chunk.append(line)
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    # Translate each chunk separately and combine results
    translated_content: list[str] = []
    for chunk in chunks:
        instructions = built_instructions(
            languages[lang_code],
            lang_code,
            sidebar_map=_extract_sidebar_translations(lang_code),
        )
        if OPENAI_MODEL.startswith("gpt-5"):
            response = openai_client.responses.create(
                model=OPENAI_MODEL,
                instructions=instructions,
                input=chunk,
                reasoning={"effort": "low"},
                text={"verbosity": "low"},
            )
            translated_content.append(response.output_text)
        elif OPENAI_MODEL.startswith("o"):
            response = openai_client.responses.create(
                model=OPENAI_MODEL,
                instructions=instructions,
                input=chunk,
            )
            translated_content.append(response.output_text)
        else:
            response = openai_client.responses.create(
                model=OPENAI_MODEL,
                instructions=instructions,
                input=chunk,
                temperature=0.0,
            )
            translated_content.append(response.output_text)

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
    if "ref/" in relative_path or not (
        file_path.endswith(".md") or file_path.endswith(".mdx")
    ):
        return

    # Determine target languages
    target_langs = (
        [code for code in ONLY_LANGS if code in languages]
        if ONLY_LANGS
        else list(languages.keys())
    )
    for lang_code in target_langs:
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
