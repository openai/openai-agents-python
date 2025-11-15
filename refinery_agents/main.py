"""Executable entry point for the refinery design review demo."""

from __future__ import annotations

import logging

from agents import Runner, SQLiteSession

from .agents import (
    create_checker_agent,
    create_document_controller_agent,
    create_instrument_engineer_agent,
    create_orchestrator_agent,
    create_piping_engineer_agent,
    create_process_engineer_agent,
    create_standards_safety_agent,
    create_workorder_drafter_agent,
)
from .config import RefineryConfig
from .context import RefineryContext
from .tools import DocumentRepository
from .work_order_schema import EngineeringWorkOrder


def run_demo(high_level_request: str | None = None) -> EngineeringWorkOrder:
    """Run the full design review workflow and return the final work order."""

    request = high_level_request or (
        "Develop an engineering work order to upgrade the existing flow meter on "
        "line 6\"-410-P-123-A to a more accurate technology, using only the available "
        "refinery documentation."
    )

    config = RefineryConfig.from_env()
    docs_repo = DocumentRepository(documents_root=config.documents_root)
    context = RefineryContext(config=config, docs_repo=docs_repo)
    context.log("Initialising multi-agent design review workflow.")

    document_controller = create_document_controller_agent(config)
    process_engineer = create_process_engineer_agent(config)
    instrument_engineer = create_instrument_engineer_agent(config)
    piping_engineer = create_piping_engineer_agent(config)
    standards_safety = create_standards_safety_agent(config)
    workorder_drafter = create_workorder_drafter_agent(config)
    checker = create_checker_agent(config)

    orchestrator = create_orchestrator_agent(
        config,
        document_controller=document_controller,
        process_engineer=process_engineer,
        instrument_engineer=instrument_engineer,
        piping_engineer=piping_engineer,
        standards_safety=standards_safety,
        workorder_drafter=workorder_drafter,
        checker=checker,
    )

    session = SQLiteSession(session_id="refinery-demo", db_path=":memory:")

    result = Runner.run_sync(
        orchestrator,
        request,
        context=context,
        session=session,
    )

    return result.final_output_as(EngineeringWorkOrder)


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    work_order = run_demo()
    print(work_order.model_dump_json(indent=2))


if __name__ == "__main__":
    _main()

