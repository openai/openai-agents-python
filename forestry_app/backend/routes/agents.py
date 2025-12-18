from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException

from models.database import UserModel
from models.schemas import AgentInfo, AgentListResponse, RoutingRequest, RoutingResponse
from services.auth import get_current_user
from agents import (
    get_all_agents,
    get_agent_by_id,
    get_agents_by_category,
    get_agent_categories,
    ForestryAgentManager
)

router = APIRouter(prefix="/agents", tags=["Agents"])


@router.get("/", response_model=AgentListResponse)
async def list_agents(
    current_user: UserModel = Depends(get_current_user)
):
    """List all available agents."""
    agents = get_all_agents()
    categories = get_agent_categories()

    agent_infos = [
        AgentInfo(
            id=agent["id"],
            name=agent["name"],
            description=agent["description"],
            category=agent["category"],
            produces=agent["produces"],
            icon=agent["icon"],
            color=agent["color"]
        )
        for agent in agents
    ]

    return AgentListResponse(agents=agent_infos, categories=categories)


@router.get("/categories")
async def list_categories(
    current_user: UserModel = Depends(get_current_user)
):
    """List all agent categories."""
    return {"categories": get_agent_categories()}


@router.get("/category/{category}")
async def get_agents_in_category(
    category: str,
    current_user: UserModel = Depends(get_current_user)
):
    """Get all agents in a specific category."""
    agents = get_agents_by_category(category)
    if not agents:
        raise HTTPException(status_code=404, detail="Category not found")

    return {
        "category": category,
        "agents": [
            AgentInfo(
                id=agent["id"],
                name=agent["name"],
                description=agent["description"],
                category=agent["category"],
                produces=agent["produces"],
                icon=agent["icon"],
                color=agent["color"]
            )
            for agent in agents
        ]
    }


@router.get("/{agent_id}")
async def get_agent(
    agent_id: str,
    current_user: UserModel = Depends(get_current_user)
):
    """Get a specific agent by ID."""
    agent = get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return AgentInfo(
        id=agent["id"],
        name=agent["name"],
        description=agent["description"],
        category=agent["category"],
        produces=agent["produces"],
        icon=agent["icon"],
        color=agent["color"]
    )


@router.post("/route", response_model=RoutingResponse)
async def route_message(
    request: RoutingRequest,
    current_user: UserModel = Depends(get_current_user)
):
    """Determine which agents should handle a message."""
    manager = ForestryAgentManager()
    result = await manager.route_message(request.message, request.context)

    return RoutingResponse(
        recommended_agents=result.get("recommended_agents", []),
        reasoning=result.get("reasoning", ""),
        confidence=result.get("confidence", 0.5)
    )


@router.get("/teams/default")
async def get_default_teams(
    current_user: UserModel = Depends(get_current_user)
):
    """Get predefined agent teams for common workflows."""
    teams = {
        "default": {
            "name": "Default Team",
            "description": "Standard routing for unclear requests (B+E+G)",
            "agents": ["data_readiness", "qa_qc", "operational_feasibility"]
        },
        "data_pipeline": {
            "name": "Data Pipeline Team",
            "description": "Full data processing workflow",
            "agents": ["data_readiness", "post_processing", "qa_qc"]
        },
        "operations": {
            "name": "Operations Team",
            "description": "Planning and execution focus",
            "agents": ["run_manager", "operational_feasibility", "communications"]
        },
        "analysis": {
            "name": "Analysis Team",
            "description": "Strategy and impact assessment",
            "agents": ["lut_threshold", "adoption_impact", "feedback_synth"]
        },
        "troubleshooting": {
            "name": "Troubleshooting Team",
            "description": "Debug and QA focus",
            "agents": ["debug_triage", "qa_qc", "data_readiness"]
        },
        "documentation": {
            "name": "Documentation Team",
            "description": "Communications and knowledge management",
            "agents": ["communications", "librarian", "feedback_synth"]
        },
        "full_team": {
            "name": "Full Team",
            "description": "All agents working together",
            "agents": [
                "run_manager", "data_readiness", "lut_threshold", "post_processing",
                "qa_qc", "debug_triage", "operational_feasibility", "feedback_synth",
                "adoption_impact", "communications", "librarian"
            ]
        }
    }

    return {"teams": teams}
