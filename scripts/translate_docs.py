# ruff: noqa
import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor

# import logging
# logging.basicConfig(level=logging.INFO)
# logging.getLogger("openai").setLevel(logging.DEBUG)

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-sol")

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
REPO_ROOT = Path(__file__).resolve().parents[2]
languages = {
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    # Add more languages here, e.g., "fr": "French"
}

# Initialize OpenAI client
api_key = os.getenv("OPENAI_API_KEY")
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
    "Agents as tools",
    "Agents-as-tools",
    # Add more terms here
]

eng_to_non_eng_mapping = {
    "ja": {
        "agents": "エージェント",
        "agent orchestration": "エージェントオーケストレーション",
        "orchestrating multiple agents": "エージェントオーケストレーション",
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
        "agent orchestration": "에이전트 오케스트레이션",
        "computer use": "컴퓨터 사용",
        "OAI hosted tools": "OpenAI 호스트하는 도구",
        "well formed data": "적절한 형식의 데이터",
        "guardrail": "가드레일",
        "orchestrating multiple agents": "에이전트 오케스트레이션",
        "handoffs": "핸드오프",
        "function tools": "함수 도구",
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
        "Python-first": "파이썬 우선",
        "interruption": "인터럽션(중단 처리)",
        "TypeScript-first": "TypeScript 우선",
        "Human in the loop": "휴먼인더루프 (HITL)",
        "Hosted tool": "호스티드 툴",
        "Hosted MCP server tools": "호스티드 MCP 서버 도구",
        "Realtime Agents": "실시간 에이전트",
        "Build your first agent in minutes.": "단 몇 분 만에 첫 에이전트를 만들 수 있습니다",
        "Let's build": "시작하기",
    },
    "zh": {
        "agents": "智能体",
        "agent orchestration": "智能体编排",
        "orchestrating multiple agents": "智能体编排",
        "computer use": "计算机操作",
        "OAI hosted tools": "由OpenAI托管的工具",
        "well formed data": "格式良好的数据",
        "guardrail": "安全防护措施",
        "handoffs": "任务转移",
        "function tools": "函数工具",
        "tracing": "追踪",
        "code examples": "代码示例",
        "vector store": "向量存储",
        "deep research": "深度研究",
        "user": "用户",
        "parameter": "参数",
        "web search": "网络检索",
        "file search": "文件检索",
        "streaming": "流式传输",
        "system prompt": "系统提示词",
        "Python first": "Python 优先",
        # Add more mappings here
    },
    # Add more languages here
}
eng_to_non_eng_instructions = {
    "common": [
        "* The term 'examples' must be code examples when the page mentions the code examples in the repo, it can be translated as either 'code examples' or 'sample code'.",
        "* The term 'primitives' can be translated as basic components.",
        "* Prefer established technical usage in the target language. Do not invent an awkward localized alternative solely to avoid an English term when that English term is standard in developer documentation.",
        "* Preserve distinctions between SDK concepts. For example, a function tool is not a tool call, a processor is not a process, and a server is not automatically a service.",
        "* In Python packaging contexts, 'extras' means installable optional-dependency extras, not dependency groups. Keep 'extras' in English when a literal translation would be unfamiliar or ambiguous.",
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
        "* 기술 문맥의 'raw'는 가공되지 않은 저수준 데이터라는 뜻입니다. 문맥에 따라 자연스럽게 번역하거나 영어 'raw'를 유지하되, 원문(source text)이라는 뜻으로 번역하지 마세요.",
        "* 'instructions', 'tools' 같은 API 매개변수와 temperature, top_p, max_tokens, presence_penalty, frequency_penalty 등은 영문 그대로 유지하세요.",
        "* 문장이 아닌 불릿 항목 끝에는 마침표를 찍지 마세요.",
    ],
    "zh": [
        "* The term 'examples' must be code examples when the page mentions the code examples in the repo, it can be translated as either 'code examples' or 'sample code'.",
        "* The term 'primitives' can be translated as basic components.",
        "* When the terms 'instructions' and 'tools' are mentioned as API parameter names, they must be kept as is.",
        "* The terms 'temperature', 'top_p', 'max_tokens', 'presence_penalty', 'frequency_penalty' as parameter names must be kept as is.",
        "* Keep the original structure like `* **The thing**: foo`; this needs to be translated as `* **(translation)**: (translation)`",
    ],
    # Add more languages here
}


def built_instructions(target_language: str, lang_code: str) -> str:
    do_not_translate_terms = "\n".join(do_not_translate)
    specific_terms = "\n".join(
        [f"* {k} -> {v}" for k, v in eng_to_non_eng_mapping.get(lang_code, {}).items()]
    )
    specific_instructions = "\n".join(
        eng_to_non_eng_instructions.get("common", [])
        + eng_to_non_eng_instructions.get(lang_code, [])
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
- Keep all placeholders such as `CODE_BLOCK_*`, `INLINE_CODE_*`, and `CODE_LINE_PREFIX` unchanged.
- Convert asset paths: `./assets/…` → `../assets/…`.  
  *Example:* `![img](./assets/pic.png)` → `![img](../assets/pic.png)`
- Treat the **Do‑Not‑Translate list** and **Term‑Specific list** as case‑insensitive; preserve the original casing you see.
- Skip translation for:
  - Inline code surrounded by single back‑ticks ( `like_this` ).
  - Fenced code blocks delimited by ``` or ~~~, including all comments inside them.
  - Link URLs inside `[label](URL)` – translate the label, never the URL.

#########################
##  HARD CONSTRAINTS   ##
#########################
- Never insert spaces immediately inside emphasis markers. Use `**bold**`, not `** bold **`.
- Preserve every source inline-code span exactly once. Do not add, remove, duplicate, split, merge, or translate inline-code spans. Keep each span with the text it describes, but move it when target-language grammar requires a different word order.
- Preserve the number of emphasis markers from the source: if the source uses `**` or `__`, keep the same pair count.
- Ensure one space after heading markers: `##Heading` -> `## Heading`.
- Ensure one space after list markers: `-Item` -> `- Item`, `*Item` -> `* Item` (does not apply to `**`).
- Trim spaces inside link/image labels: `[ Label ](url)` -> `[Label](url)`.

###########################
##  GOOD / BAD EXAMPLES  ##
###########################
- Good: This is **bold** text.
- Bad:  This is ** bold ** text.
- Good: ## Heading
- Bad:  ##Heading
- Good: - Item
- Bad:  -Item
- Good: [Label](https://example.com)
- Bad:  [ Label ](https://example.com)

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
- When translating Markdown tables, preserve the exact table structure, including all delimiters (|), header separators (---), and row/column counts. Only translate the cell contents. Do not add, remove, or reorder columns or rows.

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
3. Perform a self-review to check for the following common issues:
   - Naturalness, accuracy, and consistency throughout the text.
   - Spacing inside markdown syntax such as `*` or `_`; `**bold**` is correct whereas `** bold **` is not.
   - Unwanted spaces inside link or image labels, such as `[ Label ](url)`.
   - Headings or list markers missing a space after their marker.
4. If improvements are necessary, refine the content without changing the original meaning.
5. Continue improving the translation until you are fully satisfied with the result.
6. Once the final output is ready, return **only** the translated markdown text. No extra commentary.
"""


FENCE_OPENING_PATTERN = re.compile(r"^[ \t]*(?P<marker>`{3,}|~{3,})(?P<info>.*)$")


def opening_fence(line: str) -> tuple[str, int] | None:
    match = FENCE_OPENING_PATTERN.match(line)
    if match is None:
        return None
    marker = match.group("marker")
    if marker[0] == "`" and "`" in match.group("info"):
        return None
    return marker[0], len(marker)


def is_closing_fence(line: str, marker: str, minimum_length: int) -> bool:
    return re.fullmatch(rf"[ \t]*{re.escape(marker)}{{{minimum_length},}}[ \t]*", line) is not None


def fenced_code_ranges(markdown: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    open_fence: tuple[str, int] | None = None
    block_start = 0
    offset = 0
    for line_with_ending in markdown.splitlines(keepends=True):
        line = line_with_ending.rstrip("\r\n")
        line_end = offset + len(line)
        if open_fence is None:
            opening = opening_fence(line)
            if opening is not None:
                open_fence = opening
                block_start = offset
        elif is_closing_fence(line, *open_fence):
            ranges.append((block_start, line_end))
            open_fence = None
        offset += len(line_with_ending)
    if open_fence is not None:
        raise ValueError("Unclosed fenced code block")
    return ranges


def fenced_code_blocks(markdown: str) -> list[str]:
    return [markdown[start:end] for start, end in fenced_code_ranges(markdown)]


def remove_fenced_code_blocks(markdown: str) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end in fenced_code_ranges(markdown):
        parts.append(markdown[cursor:start])
        cursor = end
    parts.append(markdown[cursor:])
    return "".join(parts)


def protect_fenced_code(markdown: str, *, namespace: str) -> tuple[str, list[str]]:
    parts: list[str] = []
    code_blocks: list[str] = []
    cursor = 0
    for index, (start, end) in enumerate(fenced_code_ranges(markdown)):
        parts.append(markdown[cursor:start])
        parts.append(code_block_placeholder(namespace, index))
        code_blocks.append(markdown[start:end])
        cursor = end
    parts.append(markdown[cursor:])
    return "".join(parts), code_blocks


def backtick_run_end(markdown: str, start: int) -> int:
    end = start + 1
    while end < len(markdown) and markdown[end] == "`":
        end += 1
    return end


def inline_code_ranges(markdown: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(markdown):
        opener_start = markdown.find("`", cursor)
        if opener_start < 0:
            break
        opener_end = backtick_run_end(markdown, opener_start)
        delimiter_length = opener_end - opener_start
        search_from = opener_end
        matching_closer_end: int | None = None
        while search_from < len(markdown):
            closer_start = markdown.find("`", search_from)
            if closer_start < 0:
                break
            closer_end = backtick_run_end(markdown, closer_start)
            if closer_end - closer_start == delimiter_length:
                matching_closer_end = closer_end
                break
            search_from = closer_end
        if matching_closer_end is None:
            cursor = opener_end
        else:
            ranges.append((opener_start, matching_closer_end))
            cursor = matching_closer_end
    return ranges


def inline_code_spans(markdown: str) -> list[str]:
    without_fences = remove_fenced_code_blocks(markdown)
    return [without_fences[start:end] for start, end in inline_code_ranges(without_fences)]


def inline_code_spans_match(source: str, translated: str) -> bool:
    try:
        return Counter(inline_code_spans(source)) == Counter(inline_code_spans(translated))
    except ValueError:
        return False


def fenced_code_blocks_match(source: str, translated: str) -> bool:
    try:
        return fenced_code_blocks(source) == fenced_code_blocks(translated)
    except ValueError:
        return False


def placeholder_namespace(markdown: str) -> str:
    namespace_index = 0
    while True:
        namespace = f"T{namespace_index}_"
        if f"CODE_BLOCK_{namespace}" not in markdown and f"INLINE_CODE_{namespace}" not in markdown:
            return namespace
        namespace_index += 1


def code_block_placeholder(namespace: str, index: int) -> str:
    return f"CODE_BLOCK_{namespace}{index:03}"


def inline_code_placeholder(namespace: str, index: int) -> str:
    return f"`INLINE_CODE_{namespace}{index:04}`"


def restore_placeholders(markdown: str, replacements: dict[str, str]) -> str:
    if not replacements:
        return markdown
    placeholders = sorted(replacements, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(value) for value in placeholders))
    return pattern.sub(lambda match: replacements[match.group(0)], markdown)


def placeholders_preserved(markdown: str, placeholders: list[str]) -> bool:
    return all(markdown.count(placeholder) == 1 for placeholder in placeholders)


def protect_inline_code(
    markdown: str, *, namespace: str = "", start_index: int = 0
) -> tuple[str, list[str]]:
    parts: list[str] = []
    inline_codes: list[str] = []
    cursor = 0
    for start, end in inline_code_ranges(markdown):
        parts.append(markdown[cursor:start])
        parts.append(inline_code_placeholder(namespace, start_index + len(inline_codes)))
        inline_codes.append(markdown[start:end])
        cursor = end
    parts.append(markdown[cursor:])
    return "".join(parts), inline_codes


def restore_inline_code(markdown: str, inline_codes: list[str], *, namespace: str = "") -> str:
    replacements = {
        inline_code_placeholder(namespace, idx): inline_code
        for idx, inline_code in enumerate(inline_codes)
    }
    return restore_placeholders(markdown, replacements)


def restore_code_blocks(markdown: str, code_blocks: list[str], *, namespace: str) -> str:
    replacements = {
        code_block_placeholder(namespace, idx): code_block
        for idx, code_block in enumerate(code_blocks)
    }
    return restore_placeholders(markdown, replacements)


def translate_chunk(chunk: str, instructions: str) -> str:
    if OPENAI_MODEL.startswith("gpt-5"):
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            instructions=instructions,
            input=chunk,
            reasoning={"effort": "high"},
            text={"verbosity": "medium"},
        )
    elif OPENAI_MODEL.startswith("o"):
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            instructions=instructions,
            input=chunk,
        )
    else:
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            instructions=instructions,
            input=chunk,
            temperature=0.0,
        )
    return response.output_text


# Function to translate and save files
def translate_file(file_path: str, target_path: str, lang_code: str) -> None:
    print(f"Translating {file_path} into a different language: {lang_code}")
    with open(file_path, encoding="utf-8") as f:
        content = f.read()
    namespace = placeholder_namespace(content)

    if ENABLE_CODE_SNIPPET_EXCLUSION is True:
        protected_content, code_blocks = protect_fenced_code(content, namespace=namespace)
    else:
        protected_content = content
        code_blocks = []

    # Split content into lines
    lines: list[str] = protected_content.splitlines()
    chunks: list[str] = []
    current_chunk: list[str] = []

    # Split content into chunks of up to 120 lines, ensuring splits occur before section titles
    for line in lines:
        if (
            ENABLE_SMALL_CHUNK_TRANSLATION is True
            and len(current_chunk) >= 120  # required for gpt-4.5
            and line.startswith("#")
        ):
            chunks.append("\n".join(current_chunk))
            current_chunk = []
        current_chunk.append(line)
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    inline_codes: list[str] = []
    protected_chunks: list[str] = []
    for chunk in chunks:
        protected_chunk, chunk_inline_codes = protect_inline_code(
            chunk, namespace=namespace, start_index=len(inline_codes)
        )
        protected_chunks.append(protected_chunk)
        inline_codes.extend(chunk_inline_codes)
    chunks = protected_chunks

    instructions = built_instructions(languages[lang_code], lang_code)
    translated_text = ""
    for _attempt in range(3):
        translated_text = "\n".join(translate_chunk(chunk, instructions) for chunk in chunks)
        placeholders = [
            *(code_block_placeholder(namespace, idx) for idx in range(len(code_blocks))),
            *(inline_code_placeholder(namespace, idx) for idx in range(len(inline_codes))),
        ]
        if not placeholders_preserved(translated_text, placeholders):
            continue
        translated_text = restore_inline_code(translated_text, inline_codes, namespace=namespace)
        translated_text = restore_code_blocks(translated_text, code_blocks, namespace=namespace)
        if inline_code_spans_match(content, translated_text) and fenced_code_blocks_match(
            content, translated_text
        ):
            break
    else:
        raise ValueError(
            f"Protected Markdown changed after 3 translation attempts for {file_path} to {lang_code}"
        )

    # FIXME: enable mkdocs search plugin to seamlessly work with i18n plugin
    translated_text = SEARCH_EXCLUSION + translated_text
    # Save the combined translated content
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(translated_text)


def git_last_commit_timestamp(path: str) -> int:
    try:
        relative_path = os.path.relpath(path, REPO_ROOT)
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%ct", "--", relative_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return 0
        output = result.stdout.strip()
        if not output:
            return 0
        return int(output)
    except Exception:
        return 0


def should_translate_based_on_translation(file_path: str) -> bool:
    relative_path = os.path.relpath(file_path, source_dir)
    ja_path = os.path.join(source_dir, "ja", relative_path)
    en_timestamp = git_last_commit_timestamp(file_path)
    if en_timestamp == 0:
        return True
    ja_timestamp = git_last_commit_timestamp(ja_path)
    if ja_timestamp == 0:
        return True
    return ja_timestamp < en_timestamp


def translate_single_source_file(
    file_path: str, *, check_translation_outdated: bool = True
) -> None:
    relative_path = os.path.relpath(file_path, source_dir)
    if "ref/" in relative_path or not file_path.endswith(".md"):
        return
    if check_translation_outdated and not should_translate_based_on_translation(file_path):
        print(f"Skipping {file_path}: The translated one is up-to-date.")
        return

    for lang_code in languages:
        target_dir = os.path.join(source_dir, lang_code)
        target_path = os.path.join(target_dir, relative_path)

        # Ensure the target directory exists
        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        # Translate and save the file
        translate_file(file_path, target_path, lang_code)


def normalize_source_file_arg(file_arg: str) -> str:
    if file_arg.startswith(f"{source_dir}/"):
        return file_arg[len(source_dir) + 1 :]
    if os.path.isabs(file_arg):
        return os.path.relpath(file_arg, source_dir)
    return file_arg


def translate_source_files(
    file_paths: list[str], *, check_translation_outdated: bool = True
) -> None:
    unique_paths = list(dict.fromkeys(file_paths))
    if not unique_paths:
        return
    concurrency = min(6, len(unique_paths))
    if concurrency <= 1:
        translate_single_source_file(
            unique_paths[0], check_translation_outdated=check_translation_outdated
        )
        return
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                translate_single_source_file,
                path,
                check_translation_outdated=check_translation_outdated,
            )
            for path in unique_paths
        ]
        for future in futures:
            future.result()


def main():
    parser = argparse.ArgumentParser(description="Translate documentation files")
    parser.add_argument(
        "--file",
        action="append",
        type=str,
        help="Specific file to translate (relative to docs directory).",
    )
    parser.add_argument(
        "--file-list",
        type=str,
        help="Path to a newline-delimited file list to translate.",
    )
    parser.add_argument(
        "--mode",
        choices=["only-changes", "full"],
        default="only-changes",
        help="Translation mode. 'only-changes' translates only when the Japanese file is older than the English source.",
    )
    args = parser.parse_args()

    check_translation_outdated = args.mode == "only-changes"

    if args.file or args.file_list:
        file_args: list[str] = []
        if args.file:
            file_args.extend(args.file)
        if args.file_list:
            with open(args.file_list, encoding="utf-8") as f:
                file_args.extend([line.strip() for line in f.read().splitlines() if line.strip()])
        file_paths: list[str] = []
        for file_arg in file_args:
            relative_file = normalize_source_file_arg(file_arg)
            file_path = os.path.join(source_dir, relative_file)
            if os.path.exists(file_path):
                file_paths.append(file_path)
            else:
                print(f"Warning: File {file_path} does not exist; skipping.")
        if not file_paths:
            print("Error: No valid files found to translate")
            sys.exit(1)
        translate_source_files(file_paths, check_translation_outdated=check_translation_outdated)
        print("Translation completed for requested file(s)")
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
                    futures.append(
                        executor.submit(
                            translate_single_source_file,
                            filepath,
                            check_translation_outdated=check_translation_outdated,
                        )
                    )
                    if len(futures) >= concurrency:
                        for future in futures:
                            future.result()
                        futures.clear()

        print("Translation completed.")


if __name__ == "__main__":
    # translate_single_source_file("docs/index.md")
    main()
