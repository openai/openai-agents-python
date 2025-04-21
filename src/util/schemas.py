from typing import Literal, Optional, Dict, Union
from pydantic import BaseModel, Field

class NewTask(BaseModel):
    action: Literal["new_task"]
    task_type: str
    user_prompt: str
    params: Dict = Field(default_factory=dict)
    first_agent: Optional[str] = "auto"

class NewMessage(BaseModel):
    action: Literal["new_message"]
    task_id: str
    message: str
    agent_session_id: Optional[str] = None

Inbound = Union[NewTask, NewMessage]
