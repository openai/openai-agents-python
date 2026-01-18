import os
import re

from agents import Agent, Runner, ShellTool, TResponseInputItem
from examples.auto_mode import input_with_fallback, is_auto_mode
from examples.tools.shell import ShellExecutor

"""
This example demonstrates the skills pattern. It discovers skills under `skills`, lists them
in the system prompt, and lets the agent load a skill on demand. The key idea is
progressive disclosure: keep the base prompt small and only load skills when needed.

This version loads skills with the shell tool and can execute scripts.
"""

# Update this if your skills live in a different directory.
SKILL_DIR = "examples/agent_patterns/skills"

# Auto-approve shell commands for this example to avoid interactive prompts.
os.environ.setdefault("SHELL_AUTO_APPROVE", "1")


def find_all_skills(skill_dir: str) -> list[dict[str, str]]:
    """Find installed skills in the given directory."""
    skills: list[dict[str, str]] = []
    if not os.path.exists(skill_dir):
        return skills

    for entry in os.listdir(skill_dir):
        skill_md_path = os.path.join(skill_dir, entry, "SKILL.md")
        if os.path.exists(skill_md_path):
            content = open(skill_md_path, encoding="utf-8").read()
            match = re.search(r"^description:\s*(.+?)$", content, re.MULTILINE)
            description = match.group(1).strip() if match else ""
            skills.append({"name": entry, "description": description})
    return skills


def generate_skills_prompt(skills: list[dict[str, str]], skill_dir: str) -> str:
    """Generate the skills section for the system prompt."""
    if not skills:
        return ""

    # Progressive disclosure keeps the base prompt small and loads skills only when needed.
    skill_tags = "\n\n".join(
        f"""<skill>
<name>{s["name"]}</name>
<description>{s["description"]}</description>
<location>project</location>
</skill>"""
        for s in skills
    )

    return f"""<skills_system priority="1">

## Available Skills

<usage>
When users ask you to perform tasks, check if any of the available skills below can help complete the task more effectively. Skills provide specialized capabilities and domain knowledge.

How to use skills:
- Use the shell tool with bash to load a skill, for example: `bash -lc "cat {skill_dir}/<skill_name>/SKILL.md"`.
- Base directory is `{skill_dir}/<skill_name>` for resolving bundled resources (references/, scripts/, assets/).
- To execute a bash script, use `bash -lc "bash {skill_dir}/<skill_name>/scripts/<script>.sh ..."` to avoid permission errors.
- To read other skill files, use `bash -lc "cat {skill_dir}/<skill_name>/<path>"`.

Usage notes:
- Only use skills listed in <available_skills> below.
- Do not invoke a skill that is already loaded in your context.
- Use progressive disclosure: load a skill only when it is needed.
</usage>

<available_skills>

{skill_tags}

</available_skills>

</skills_system>"""


def create_agent_with_skills(base_instructions: str, skill_dir: str = SKILL_DIR) -> Agent:
    """Create an agent with the skills system embedded in its instructions."""
    skills = find_all_skills(skill_dir)
    skills_prompt = generate_skills_prompt(skills, skill_dir)
    instructions = f"{base_instructions}\n\n{skills_prompt}" if skills_prompt else base_instructions

    return Agent(
        name="Assistant",
        instructions=instructions,
        model="gpt-5.2",
        tools=[ShellTool(executor=ShellExecutor())],
    )


# Create the agent once so the run loop can reuse it.
agent = create_agent_with_skills(
    "You are a helpful assistant. Use the shell tool to read skills and run scripts when needed."
)


async def main() -> None:
    input_data: list[TResponseInputItem] = []
    auto_mode = is_auto_mode()

    while True:
        user_input = input_with_fallback(
            "Enter a message: ",
            "Use the ascii_art skill to draw an ASCII tree that is 21 characters wide and 12 lines tall.",
        )
        input_data.append(
            {
                "role": "user",
                "content": user_input,
            }
        )

        result = await Runner.run(agent, input_data)
        print(result.final_output)

        input_data = result.to_input_list()

        if auto_mode:
            break


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
