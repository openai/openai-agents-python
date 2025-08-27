from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from agents.agent import Agent
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
def generate_tests(self, test_cases: list[TestCase]) -> None:
    """Launch a Streamlit app to view and edit test cases."""

    try:
        import importlib

        st = importlib.import_module("streamlit")
    except Exception as exc:  # pragma: no cover - streamlit may not be installed
        raise ImportError("streamlit is required to use 'generate_tests'") from exc

    if "test_cases" not in st.session_state:
        st.session_state.test_cases = [replace(tc) for tc in test_cases]

    st.title(f"Tests for {self.name}")

    for idx, case in enumerate(st.session_state.test_cases):
        with st.expander(case.name, expanded=True):
            case.name = st.text_input(
                "Name",
                value=case.name,
                key=f"name_{idx}",
            )
            case.scenario = st.text_area(
                "Scenario",
                value=case.scenario,
                key=f"scenario_{idx}",
            )

            scoring = case.scoring_config
            scoring.ground_truth = st.text_area(
                "Ground truth",
                value=str(scoring.ground_truth or ""),
                key=f"ground_{idx}",
            )
            scoring.criteria = st.text_area(
                "Criteria",
                value=scoring.criteria or "",
                key=f"criteria_{idx}",
            )
            scoring.type = st.selectbox(
                "Type",
                [
                    "tool_name",
                    "tool_argument",
                    "handoff",
                    "model_graded",
                    None,
                ],
                index=[
                    "tool_name",
                    "tool_argument",
                    "handoff",
                    "model_graded",
                    None,
                ].index(scoring.type),
                key=f"type_{idx}",
            )

    st.divider()
    st.header("Add new test case")
    new_name = st.text_input("New test name", key="new_name")
    new_scenario = st.text_area("New test scenario", key="new_scenario")
    if st.button("Add Test Case"):
        st.session_state.test_cases.append(
            TestCase(
                name=new_name or f"test-{len(st.session_state.test_cases) + 1}",
                scenario=new_scenario,
                scoring_config=ScoringConfig(),
                agent_to_test=self,
            )
        )
        st.experimental_rerun()

    test_cases[:] = st.session_state.test_cases
