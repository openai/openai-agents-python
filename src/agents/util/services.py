from models import Task, AgentSession, Message   # your ORM models
from utils.webhook import post_webhook, STRUCTURED_URL, CLARIFICATION_URL
from agents.runner import run_agent, decide_session  # you already have

async def handle_new_task(p):
    task = Task.create(user_id=p.request_user.id,  # provided by auth
                       title=p.user_prompt[:40],
                       type=p.task_type,
                       status="pending",
                       params=p.params)
    first_def = "manager" if p.first_agent == "auto" else p.first_agent
    session = AgentSession.create(task=task,
                                  agent_definition=first_def,
                                  status="running")
    Message.create(task=task, role="user", content=p.user_prompt)

    await run_agent(session)          # async call to your agent loop
    return {"task_id": task.id}

async def handle_new_message(p):
    Message.create(task_id=p.task_id,
                   agent_session_id=p.agent_session_id,
                   role="user",
                   content=p.message)

    session = (AgentSession.get(p.agent_session_id)
               if p.agent_session_id
               else decide_session(p.task_id))
    await run_agent(session)
    return {"ok": True}
