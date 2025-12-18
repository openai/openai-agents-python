from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


# User schemas
class UserCreate(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: int
    username: str
    created_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    username: Optional[str] = None


# Re-export for backward compatibility
User = UserResponse


# Chat schemas
class ChatCreate(BaseModel):
    title: Optional[str] = "New Chat"
    agent_ids: List[str] = Field(default_factory=list)


class MessageCreate(BaseModel):
    content: str
    agent_ids: Optional[List[str]] = None  # Which agents to route to


class MessageResponse(BaseModel):
    id: int
    chat_id: int
    role: str
    content: str
    agent_id: Optional[str]
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    class Config:
        from_attributes = True


class ChatResponse(BaseModel):
    id: int
    user_id: int
    title: str
    agent_ids: List[str]
    created_at: datetime
    updated_at: datetime
    is_archived: bool
    messages: List[MessageResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


class ChatListResponse(BaseModel):
    id: int
    title: str
    agent_ids: List[str]
    created_at: datetime
    updated_at: datetime
    message_count: int = 0

    class Config:
        from_attributes = True


# Re-export
Chat = ChatResponse
Message = MessageResponse


# Plan schemas
class PlanCreate(BaseModel):
    title: str
    description: Optional[str] = None
    agent_ids: List[str] = Field(default_factory=list)
    content: Dict[str, Any] = Field(default_factory=dict)


class PlanResponse(BaseModel):
    id: int
    user_id: int
    title: str
    description: Optional[str]
    agent_ids: List[str]
    status: str
    content: Dict[str, Any]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PlanListResponse(BaseModel):
    id: int
    title: str
    description: Optional[str]
    status: str
    agent_ids: List[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Re-export
Plan = PlanResponse


# Agent Task schemas
class AgentTaskCreate(BaseModel):
    agent_id: str
    task_type: str
    input_data: Dict[str, Any] = Field(default_factory=dict)


class AgentTaskResponse(BaseModel):
    id: int
    plan_id: int
    agent_id: str
    task_type: str
    input_data: Dict[str, Any]
    output_data: Dict[str, Any]
    status: str
    created_at: datetime
    completed_at: Optional[datetime]

    class Config:
        from_attributes = True


# Re-export
AgentTask = AgentTaskResponse


# Agent schemas
class AgentInfo(BaseModel):
    id: str
    name: str
    description: str
    category: str
    produces: List[str]
    icon: str
    color: str


class AgentListResponse(BaseModel):
    agents: List[AgentInfo]
    categories: List[str]


# Routing schemas
class RoutingRequest(BaseModel):
    message: str
    context: Optional[str] = None


class RoutingResponse(BaseModel):
    recommended_agents: List[str]
    reasoning: str
    confidence: float
