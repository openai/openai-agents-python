from __future__ import annotations

import asyncio
from dataclasses import replace
from textwrap import dedent
from typing import TYPE_CHECKING, Any, Callable, TypeVar

import streamlit as st

from agents.agent import Agent
from examples.hackathon.conversation_generator import (
    generate_conversation,
    generate_simulated_agent,
    generate_simulated_user,
)
from examples.hackathon.example_agents import customer_service_agent
from examples.hackathon.types import ScoringConfig, TestCase

if TYPE_CHECKING:  # pragma: no cover - only for type checkers
    pass

F = TypeVar("F", bound=Callable[..., Any])


def add_method(cls: type[Any]) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        setattr(cls, func.__name__, func)
        return func

    return decorator


@add_method(Agent)
def generate_evals(self, test_cases: list[TestCase]) -> None:
    """Edit tests and generate synthetic conversations inline; evals stubbed."""

    # Initialize session state
    if "test_cases" not in st.session_state:
        st.session_state.test_cases = [replace(tc) for tc in test_cases]
    if "generated_conversations" not in st.session_state:
        # map index -> list[TResponseInputItem]
        st.session_state.generated_conversations = {}
    if "generating" not in st.session_state:
        # map index -> bool to indicate per-test generation in progress
        st.session_state.generating = {}
    if "generating_all" not in st.session_state:
        st.session_state.generating_all = False

    # Ensure wide layout once
    if "_wide_set" not in st.session_state:
        try:
            st.set_page_config(layout="wide")
        except RuntimeError:
            pass
        st.session_state._wide_set = True

    st.title(f"Tests for {self.name}")

    # Global generate-all button
    global_disabled = st.session_state.generating_all or any(st.session_state.generating.values())
    if st.button("Generate All Conversations", key="gen_all", disabled=global_disabled):
        st.session_state.generating_all = True
        for idx in range(len(st.session_state.test_cases)):
            st.session_state.generating[idx] = True
        # Run generation synchronously
        for idx, case in enumerate(st.session_state.test_cases):
            simulated_user = generate_simulated_user(case.scenario, user_prompt="")
            simulated_agent = generate_simulated_agent(case.scenario, case.agent_to_test)
            convo = asyncio.run(generate_conversation(case, simulated_agent, simulated_user))
            st.session_state.generated_conversations[idx] = convo
            st.session_state.generating[idx] = False
        st.session_state.generating_all = False

    type_options = ["tool_name", "tool_argument", "handoff", "model_graded", None]

    for idx, case in enumerate(st.session_state.test_cases):
        with st.expander(case.name, expanded=True):
            col_left, col_right = st.columns([3, 4])
            with col_left:
                case.name = st.text_input("Name", value=case.name, key=f"name_{idx}")
                case.scenario = st.text_area("Scenario", value=case.scenario, key=f"scenario_{idx}")

                scoring = case.scoring_config
                current_type = scoring.type if scoring.type in type_options else None
                selected_index = type_options.index(current_type)
                scoring.type = st.selectbox(
                    "Type",
                    type_options,
                    index=selected_index,
                    key=f"type_{idx}",
                )
                if scoring.type == "model_graded":
                    scoring.criteria = st.text_area(
                        "Criteria",
                        value=scoring.criteria or "",
                        key=f"criteria_{idx}",
                    )
                    scoring.ground_truth = None
                else:
                    scoring.ground_truth = st.text_area(
                        "Ground truth",
                        value=str(scoring.ground_truth or ""),
                        key=f"ground_{idx}",
                    )
                    scoring.criteria = None

                disabled = st.session_state.generating_all or st.session_state.generating.get(idx, False)
                if st.button("Generate Conversation", key=f"gen_{idx}", disabled=disabled):
                    st.session_state.generating[idx] = True
                    simulated_user = generate_simulated_user(case.scenario, user_prompt="")
                    simulated_agent = generate_simulated_agent(case.scenario, case.agent_to_test)
                    convo = asyncio.run(generate_conversation(case, simulated_agent, simulated_user))
                    st.session_state.generated_conversations[idx] = convo
                    st.session_state.generating[idx] = False

            with col_right:
                st.markdown("**Generated Conversation**")
                if st.session_state.generating_all or st.session_state.generating.get(idx, False):
                    st.write("Generating...")
                conv = st.session_state.generated_conversations.get(idx)
                if conv:
                    st.json(conv)
                elif not (st.session_state.generating_all or st.session_state.generating.get(idx, False)):
                    st.caption("No conversation generated yet.")

    st.divider()
    st.header("Add new test case")
    new_name = st.text_input("New test name", key="new_name")
    new_scenario = st.text_area("New test scenario", key="new_scenario")
    new_type = st.selectbox("Type", type_options, index=type_options.index(None), key="new_type")
    new_criteria = None
    new_ground_truth = None
    if new_type == "model_graded":
        new_criteria = st.text_area("Criteria", value="", key="new_criteria")
    else:
        new_ground_truth = st.text_area("Ground truth", value="", key="new_ground")

    if st.button("Add Test Case"):
        st.session_state.test_cases.append(
            TestCase(
                name=new_name or f"test-{len(st.session_state.test_cases) + 1}",
                scenario=new_scenario,
                scoring_config=ScoringConfig(
                    type=new_type,
                    ground_truth=new_ground_truth,
                    criteria=new_criteria,
                ),
                agent_to_test=self,
            )
        )
        st.rerun()

    st.divider()
    if st.button("Run Evals (stub)"):
        st.info("Eval execution stubbed. Implement evaluation logic here.")

    # Sync back to original list
    test_cases[:] = st.session_state.test_cases


if __name__ == "__main__":
    scenarios = [
        TestCase(
            name="escalate to manager",
            scenario=dedent("""
            1. User says hi and requests a refund
            2. Agent asks for order ID
            3. User does not answer the question and instead gets angry and requests to speak to a manager
            """),
            agent_to_test=customer_service_agent,
            scoring_config=ScoringConfig(
                type="handoff",
                ground_truth="handoff_to_manager"
            ),
        ),
    ]

    customer_service_agent.generate_tests(scenarios)
