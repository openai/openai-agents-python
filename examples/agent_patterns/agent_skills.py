import os
import re

from agents import Agent, Runner, TResponseInputItem, function_tool
from examples.auto_mode import input_with_fallback, is_auto_mode

"""
This example demonstrates the skills pattern. It discovers skills under `skills`, lists them
in the system prompt, and lets the agent load a skill on demand. The key idea is
progressive disclosure: keep the base prompt small and only load skills when needed.

This version loads skills with a Python function tool.
"""

# Update this if your skills live in a different directory.
SKILL_DIR = "examples/agent_patterns/skills"


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


def generate_skills_prompt(skills: list[dict[str, str]]) -> str:
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
- Use the load_skill tool with the skill name to load detailed instructions
- The skill content will provide step-by-step guidance for the task
- Base directory is provided for resolving bundled resources (references/, scripts/, assets/)

Usage notes:
- Only use skills listed in <available_skills> below
- Do not invoke a skill that is already loaded in your context
- Use progressive disclosure: load a skill only when it is needed
</usage>

<available_skills>

{skill_tags}

</available_skills>

</skills_system>"""


@function_tool
async def load_skill(skill_name: str) -> str:
    """Load a skill by name and return the full content."""
    print(f"Loading skill: {skill_name}")
    skill_dir = os.path.join(SKILL_DIR, skill_name)
    skill_path = os.path.join(skill_dir, "SKILL.md")
    content = open(skill_path, encoding="utf-8").read()
    return f"Reading: {skill_name}\nBase directory: {skill_dir}\n\n{content}\n\nSkill read: {skill_name}"


def create_agent_with_skills(base_instructions: str, skill_dir: str = SKILL_DIR) -> Agent:
    """Create an agent with the skills system embedded in its instructions."""
    skills = find_all_skills(skill_dir)
    skills_prompt = generate_skills_prompt(skills)
    instructions = f"{base_instructions}\n\n{skills_prompt}" if skills_prompt else base_instructions

    return Agent(
        name="Assistant",
        instructions=instructions,
        model="gpt-5-mini",
        tools=[load_skill],
    )


# Create the agent once so the run loop can reuse it.
agent = create_agent_with_skills("You are a helpful assistant.")


async def main():
    input_data: list[TResponseInputItem] = []
    auto_mode = is_auto_mode()

    while True:
        user_input = input_with_fallback(
            "Enter a message: ",
            "Use support_transcript_summary skill. Summarize this support transcript and list next steps: Customer: The app crashes after login. Agent: Asked to reinstall. Customer: Still crashes.",
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
