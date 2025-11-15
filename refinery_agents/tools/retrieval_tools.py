"""Function tools that expose the document repository to agents."""

from __future__ import annotations

from typing import Any

from agents import function_tool
from agents.tool_context import ToolContext

from ..context import RefineryContext
from .docs_repository import DocumentReference


def _format_documents(documents: list[DocumentReference]) -> list[dict[str, Any]]:
    return [doc.to_dict() for doc in documents]


def _summarize(
    doc_type: str, documents: list[DocumentReference], criteria: dict[str, Any]
) -> str:
    filters = ", ".join(
        f"{key}={value}" for key, value in criteria.items() if value not in (None, "")
    )
    base = f"Retrieved {len(documents)} {doc_type} reference(s)"
    return f"{base} using filters [{filters}]" if filters else base


@function_tool
def get_pids(
    ctx: ToolContext[RefineryContext],
    unit: str | None = None,
    equipment_tag: str | None = None,
) -> dict[str, Any]:
    """Access piping & instrumentation diagrams (P&IDs), an allowed document source."""

    documents = ctx.context.docs_repo.find_pids(unit=unit, equipment_tag=equipment_tag)
    return {
        "documents": _format_documents(documents),
        "summary": _summarize("P&ID", documents, {"unit": unit, "equipment_tag": equipment_tag}),
    }


@function_tool
def get_pfds(ctx: ToolContext[RefineryContext], unit: str | None = None) -> dict[str, Any]:
    """Access process flow diagrams (PFDs), an allowed document source."""

    documents = ctx.context.docs_repo.find_pfds(unit=unit)
    return {
        "documents": _format_documents(documents),
        "summary": _summarize("PFD", documents, {"unit": unit}),
    }


@function_tool
def get_line_list(
    ctx: ToolContext[RefineryContext],
    line_id: str,
) -> dict[str, Any]:
    """Access line lists, an allowed document source for process data."""

    documents = ctx.context.docs_repo.find_line_list(line_id=line_id)
    return {
        "documents": _format_documents(documents),
        "summary": _summarize("LineList", documents, {"line_id": line_id}),
    }


@function_tool
def get_instrument_datasheet(
    ctx: ToolContext[RefineryContext],
    tag: str,
) -> dict[str, Any]:
    """Access instrument datasheets/spec sheets, an allowed document source."""

    documents = ctx.context.docs_repo.find_instrument_datasheet(tag=tag)
    return {
        "documents": _format_documents(documents),
        "summary": _summarize("InstrumentDatasheet", documents, {"tag": tag}),
    }


@function_tool
def get_vendor_manual(
    ctx: ToolContext[RefineryContext],
    model: str,
) -> dict[str, Any]:
    """Access vendor manuals and parts catalogues, an allowed document source."""

    documents = ctx.context.docs_repo.find_vendor_manual(model=model)
    return {
        "documents": _format_documents(documents),
        "summary": _summarize("VendorManual", documents, {"model": model}),
    }


@function_tool
def get_pipe_class(
    ctx: ToolContext[RefineryContext],
    service: str,
    size: str,
) -> dict[str, Any]:
    """Access pipe class specifications, an allowed mechanical reference."""

    documents = ctx.context.docs_repo.find_pipe_class(service=service, size=size)
    return {
        "documents": _format_documents(documents),
        "summary": _summarize("PipeClass", documents, {"service": service, "size": size}),
    }


@function_tool
def get_isometrics(
    ctx: ToolContext[RefineryContext],
    identifier: str,
) -> dict[str, Any]:
    """Access piping isometrics, an allowed mechanical source for layout constraints."""

    documents = ctx.context.docs_repo.find_isometrics(identifier=identifier)
    return {
        "documents": _format_documents(documents),
        "summary": _summarize("Isometric", documents, {"identifier": identifier}),
    }


@function_tool
def get_jb_cable_schedule(
    ctx: ToolContext[RefineryContext],
    junction_box: str,
) -> dict[str, Any]:
    """Access JB/cable schedules, an allowed source for wiring information."""

    documents = ctx.context.docs_repo.find_jb_cable_schedule(junction_box=junction_box)
    return {
        "documents": _format_documents(documents),
        "summary": _summarize("JBCableSchedule", documents, {"junction_box": junction_box}),
    }


@function_tool
def get_engineering_standards(
    ctx: ToolContext[RefineryContext],
    query: str,
) -> dict[str, Any]:
    """Access engineering standards (API/ISA/company), an allowed standards source."""

    documents = ctx.context.docs_repo.find_engineering_standards(query=query)
    return {
        "documents": _format_documents(documents),
        "summary": _summarize("Standard", documents, {"query": query}),
    }

