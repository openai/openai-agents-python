from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload
from datetime import datetime

from models.database import get_db, UserModel, PlanModel, AgentTaskModel
from models.schemas import (
    PlanCreate, PlanResponse, PlanListResponse,
    AgentTaskCreate, AgentTaskResponse
)
from services.auth import get_current_user
from agents import ForestryAgentManager

router = APIRouter(prefix="/plans", tags=["Plans"])


@router.get("/", response_model=List[PlanListResponse])
async def list_plans(
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """List all plans for the current user."""
    query = select(PlanModel).where(PlanModel.user_id == current_user.id)

    if status:
        query = query.where(PlanModel.status == status)

    query = query.order_by(desc(PlanModel.updated_at)).offset(skip).limit(limit)
    result = await db.execute(query)
    plans = result.scalars().all()

    return [
        PlanListResponse(
            id=plan.id,
            title=plan.title,
            description=plan.description,
            status=plan.status,
            agent_ids=plan.agent_ids or [],
            created_at=plan.created_at,
            updated_at=plan.updated_at
        )
        for plan in plans
    ]


@router.post("/", response_model=PlanResponse)
async def create_plan(
    plan_data: PlanCreate,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Create a new plan."""
    plan = PlanModel(
        user_id=current_user.id,
        title=plan_data.title,
        description=plan_data.description,
        agent_ids=plan_data.agent_ids or [],
        content=plan_data.content or {},
        status="draft"
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)

    return PlanResponse(
        id=plan.id,
        user_id=plan.user_id,
        title=plan.title,
        description=plan.description,
        agent_ids=plan.agent_ids or [],
        status=plan.status,
        content=plan.content or {},
        created_at=plan.created_at,
        updated_at=plan.updated_at
    )


@router.get("/{plan_id}", response_model=PlanResponse)
async def get_plan(
    plan_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get a specific plan."""
    result = await db.execute(
        select(PlanModel)
        .options(selectinload(PlanModel.tasks))
        .where(PlanModel.id == plan_id)
        .where(PlanModel.user_id == current_user.id)
    )
    plan = result.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    return PlanResponse(
        id=plan.id,
        user_id=plan.user_id,
        title=plan.title,
        description=plan.description,
        agent_ids=plan.agent_ids or [],
        status=plan.status,
        content=plan.content or {},
        created_at=plan.created_at,
        updated_at=plan.updated_at
    )


@router.put("/{plan_id}", response_model=PlanResponse)
async def update_plan(
    plan_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    agent_ids: Optional[List[str]] = None,
    status: Optional[str] = None,
    content: Optional[dict] = None,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Update a plan."""
    result = await db.execute(
        select(PlanModel)
        .where(PlanModel.id == plan_id)
        .where(PlanModel.user_id == current_user.id)
    )
    plan = result.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    if title is not None:
        plan.title = title
    if description is not None:
        plan.description = description
    if agent_ids is not None:
        plan.agent_ids = agent_ids
    if status is not None:
        plan.status = status
    if content is not None:
        plan.content = content

    plan.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(plan)

    return PlanResponse(
        id=plan.id,
        user_id=plan.user_id,
        title=plan.title,
        description=plan.description,
        agent_ids=plan.agent_ids or [],
        status=plan.status,
        content=plan.content or {},
        created_at=plan.created_at,
        updated_at=plan.updated_at
    )


@router.delete("/{plan_id}")
async def delete_plan(
    plan_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Delete a plan."""
    result = await db.execute(
        select(PlanModel)
        .where(PlanModel.id == plan_id)
        .where(PlanModel.user_id == current_user.id)
    )
    plan = result.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    await db.delete(plan)
    await db.commit()

    return {"message": "Plan deleted successfully"}


@router.post("/{plan_id}/tasks", response_model=AgentTaskResponse)
async def add_task_to_plan(
    plan_id: int,
    task_data: AgentTaskCreate,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Add a task to a plan."""
    result = await db.execute(
        select(PlanModel)
        .where(PlanModel.id == plan_id)
        .where(PlanModel.user_id == current_user.id)
    )
    plan = result.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    task = AgentTaskModel(
        plan_id=plan_id,
        agent_id=task_data.agent_id,
        task_type=task_data.task_type,
        input_data=task_data.input_data or {},
        status="pending"
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    return AgentTaskResponse(
        id=task.id,
        plan_id=task.plan_id,
        agent_id=task.agent_id,
        task_type=task.task_type,
        input_data=task.input_data or {},
        output_data=task.output_data or {},
        status=task.status,
        created_at=task.created_at,
        completed_at=task.completed_at
    )


@router.get("/{plan_id}/tasks", response_model=List[AgentTaskResponse])
async def get_plan_tasks(
    plan_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get all tasks for a plan."""
    result = await db.execute(
        select(PlanModel)
        .where(PlanModel.id == plan_id)
        .where(PlanModel.user_id == current_user.id)
    )
    plan = result.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    result = await db.execute(
        select(AgentTaskModel).where(AgentTaskModel.plan_id == plan_id)
    )
    tasks = result.scalars().all()

    return [
        AgentTaskResponse(
            id=task.id,
            plan_id=task.plan_id,
            agent_id=task.agent_id,
            task_type=task.task_type,
            input_data=task.input_data or {},
            output_data=task.output_data or {},
            status=task.status,
            created_at=task.created_at,
            completed_at=task.completed_at
        )
        for task in tasks
    ]


@router.post("/{plan_id}/execute")
async def execute_plan(
    plan_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Execute all pending tasks in a plan."""
    result = await db.execute(
        select(PlanModel)
        .options(selectinload(PlanModel.tasks))
        .where(PlanModel.id == plan_id)
        .where(PlanModel.user_id == current_user.id)
    )
    plan = result.scalar_one_or_none()

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    # Update plan status
    plan.status = "active"
    await db.commit()

    manager = ForestryAgentManager()
    results = []

    for task in plan.tasks:
        if task.status == "pending":
            task.status = "in_progress"
            await db.commit()

            try:
                # Run the agent
                response = await manager.run_agent(
                    [task.agent_id],
                    f"Task: {task.task_type}\nInput: {task.input_data}"
                )

                task.output_data = {"response": response}
                task.status = "completed"
                task.completed_at = datetime.utcnow()
                results.append({
                    "task_id": task.id,
                    "status": "completed",
                    "output": response[:500]  # Truncate for response
                })
            except Exception as e:
                task.status = "failed"
                task.output_data = {"error": str(e)}
                results.append({
                    "task_id": task.id,
                    "status": "failed",
                    "error": str(e)
                })

            await db.commit()

    # Check if all tasks completed
    all_completed = all(t.status in ["completed", "failed"] for t in plan.tasks)
    if all_completed:
        plan.status = "completed"
        await db.commit()

    return {
        "plan_id": plan_id,
        "status": plan.status,
        "task_results": results
    }
