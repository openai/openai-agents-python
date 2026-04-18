import re
from pathlib import Path

file_path = Path(r"c:\Users\hp\OneDrive\Desktop\contributions\contributions\openai-agents-python\src\agents\run_internal\run_loop.py")
content = file_path.read_text(encoding="utf-8")

# 1. Fix on_llm_start in start_streaming or get_new_response
# Patterns to look for:
# await asyncio.gather(
#     hooks.on_llm_start(context_wrapper, public_agent, filtered.instructions, filtered.input),
#     (
#         public_agent.hooks.on_llm_start(
#             context_wrapper,

def repl_llm_start(match):
    indent = match.group(1)
    # We want to insert llm_context definition before await
    res = f"{indent}llm_context = LLMContext.from_run_context(\n"
    res += f"{indent}    context_wrapper,\n"
    res += f"{indent}    system_prompt=filtered.instructions,\n"
    res += f"{indent}    input_items=filtered.input,\n"
    res += f"{indent})\n"
    res += f"{indent}await asyncio.gather(\n"
    res += f"{indent}    hooks.on_llm_start(llm_context, public_agent, filtered.instructions, filtered.input),\n"
    res += f"{indent}    (\n"
    res += f"{indent}        public_agent.hooks.on_llm_start(\n"
    res += f"{indent}            llm_context,\n"
    return res

# Regex for the start hook
# This matches the pattern regardless of specific instruction/input variable names if we are careful, 
# but I'll be specific to avoid false positives.
pattern_start = re.compile(
    r"(\s+)await asyncio\.gather\(\n"
    r"\s+hooks\.on_llm_start\(context_wrapper, public_agent, filtered\.instructions, filtered\.input\),\n"
    r"\s+\(\n"
    r"\s+public_agent\.hooks\.on_llm_start\(\n"
    r"\s+context_wrapper,"
)

content = pattern_start.sub(repl_llm_start, content)

# 2. Fix the on_llm_end call that might still be using llm_context without definition (if I missed it)
# Actually, I already updated the on_llm_end call in the previous multi_replace, but it might be missing llm_context in AgentHooks call.

# 3. Ensure StepContext and LLMContext are imported
if "from ..llm_context import LLMContext" not in content:
    content = content.replace("from ..lifecycle import RunHooks", "from ..lifecycle import RunHooks\nfrom ..llm_context import LLMContext")

if "from ..step_context import StepContext" not in content:
    content = content.replace("from ..llm_context import LLMContext", "from ..llm_context import LLMContext\nfrom ..step_context import StepContext")

file_path.write_text(content, encoding="utf-8")
print("Successfully updated run_loop.py using regex")
