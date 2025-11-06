from typing import Annotated

from agents import Agent, MaxTurnsExceeded, Runner, function_tool


@function_tool
def gather_facts(topic: Annotated[str, "The topic to investigate"]) -> str:
    """Return placeholder research that simulates a tool lookup."""
    return (
        f"Key facts about {topic}: it moves through evaporation, condensation, "
        "precipitation, and collection."
    )


def main():
    agent = Agent(
        name="Researcher",
        instructions=(
            "You must call the gather_facts tool before answering. "
            "Once you have the tool output, summarize it in your own words."
        ),
        tools=[gather_facts],
    )

    try:
        Runner.run_sync(
            agent,
            input="Give me the main stages of the water cycle.",
            max_turns=1,
        )
    except MaxTurnsExceeded as max_turns_exc:
        print("Reached the max turn limit. Asking the agent to finalize without tools...\n")
        result = max_turns_exc.resume_sync(
            "Finish the answer using the gathered information without calling tools again."
        )
        print(result.final_output)
        # The water cycle proceeds through evaporation, condensation, precipitation, and collection.


if __name__ == "__main__":
    main()
