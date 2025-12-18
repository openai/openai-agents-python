from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from sqlalchemy.orm import selectinload
import json
from datetime import datetime

from models.database import get_db, UserModel, ChatModel, MessageModel
from models.schemas import (
    ChatCreate, ChatResponse, ChatListResponse,
    MessageCreate, MessageResponse
)
from services.auth import get_current_user
from agents import ForestryAgentManager

router = APIRouter(prefix="/chats", tags=["Chats"])


@router.get("/", response_model=List[ChatListResponse])
async def list_chats(
    skip: int = 0,
    limit: int = 50,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """List all chats for the current user."""
    # Query chats with message count
    result = await db.execute(
        select(
            ChatModel,
            func.count(MessageModel.id).label("message_count")
        )
        .outerjoin(MessageModel)
        .where(ChatModel.user_id == current_user.id)
        .where(ChatModel.is_archived == False)
        .group_by(ChatModel.id)
        .order_by(desc(ChatModel.updated_at))
        .offset(skip)
        .limit(limit)
    )

    chats = []
    for row in result.all():
        chat = row[0]
        message_count = row[1]
        chats.append(ChatListResponse(
            id=chat.id,
            title=chat.title,
            agent_ids=chat.agent_ids or [],
            created_at=chat.created_at,
            updated_at=chat.updated_at,
            message_count=message_count
        ))

    return chats


@router.post("/", response_model=ChatResponse)
async def create_chat(
    chat_data: ChatCreate,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Create a new chat."""
    chat = ChatModel(
        user_id=current_user.id,
        title=chat_data.title or "New Chat",
        agent_ids=chat_data.agent_ids or []
    )
    db.add(chat)
    await db.commit()
    await db.refresh(chat)

    return ChatResponse(
        id=chat.id,
        user_id=chat.user_id,
        title=chat.title,
        agent_ids=chat.agent_ids or [],
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        is_archived=chat.is_archived,
        messages=[]
    )


@router.get("/{chat_id}", response_model=ChatResponse)
async def get_chat(
    chat_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get a specific chat with all messages."""
    result = await db.execute(
        select(ChatModel)
        .options(selectinload(ChatModel.messages))
        .where(ChatModel.id == chat_id)
        .where(ChatModel.user_id == current_user.id)
    )
    chat = result.scalar_one_or_none()

    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    return ChatResponse(
        id=chat.id,
        user_id=chat.user_id,
        title=chat.title,
        agent_ids=chat.agent_ids or [],
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        is_archived=chat.is_archived,
        messages=[
            MessageResponse(
                id=msg.id,
                chat_id=msg.chat_id,
                role=msg.role,
                content=msg.content,
                agent_id=msg.agent_id,
                metadata=msg.metadata or {},
                created_at=msg.created_at
            )
            for msg in sorted(chat.messages, key=lambda x: x.created_at)
        ]
    )


@router.put("/{chat_id}")
async def update_chat(
    chat_id: int,
    title: Optional[str] = None,
    agent_ids: Optional[List[str]] = None,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Update a chat's title or agent configuration."""
    result = await db.execute(
        select(ChatModel)
        .where(ChatModel.id == chat_id)
        .where(ChatModel.user_id == current_user.id)
    )
    chat = result.scalar_one_or_none()

    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    if title is not None:
        chat.title = title
    if agent_ids is not None:
        chat.agent_ids = agent_ids

    chat.updated_at = datetime.utcnow()
    await db.commit()

    return {"message": "Chat updated successfully"}


@router.delete("/{chat_id}")
async def delete_chat(
    chat_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Delete a chat."""
    result = await db.execute(
        select(ChatModel)
        .where(ChatModel.id == chat_id)
        .where(ChatModel.user_id == current_user.id)
    )
    chat = result.scalar_one_or_none()

    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    await db.delete(chat)
    await db.commit()

    return {"message": "Chat deleted successfully"}


@router.post("/{chat_id}/messages", response_model=MessageResponse)
async def send_message(
    chat_id: int,
    message_data: MessageCreate,
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Send a message and get a response from the agents."""
    # Get the chat
    result = await db.execute(
        select(ChatModel)
        .options(selectinload(ChatModel.messages))
        .where(ChatModel.id == chat_id)
        .where(ChatModel.user_id == current_user.id)
    )
    chat = result.scalar_one_or_none()

    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Save user message
    user_message = MessageModel(
        chat_id=chat_id,
        role="user",
        content=message_data.content,
        metadata={}
    )
    db.add(user_message)
    await db.commit()
    await db.refresh(user_message)

    # Determine which agents to use
    agent_ids = message_data.agent_ids or chat.agent_ids or []

    # If no agents specified, route the message
    manager = ForestryAgentManager()
    if not agent_ids:
        routing = await manager.route_message(message_data.content)
        agent_ids = routing.get("recommended_agents", ["data_readiness", "qa_qc", "operational_feasibility"])

    # Update chat agents if changed
    if agent_ids != chat.agent_ids:
        chat.agent_ids = agent_ids

    # Get chat history for context
    chat_history = [
        {"role": msg.role, "content": msg.content}
        for msg in sorted(chat.messages, key=lambda x: x.created_at)
    ]

    # Run the agents
    response_content = await manager.run_agent(agent_ids, message_data.content, chat_history)

    # Save assistant message
    assistant_message = MessageModel(
        chat_id=chat_id,
        role="assistant",
        content=response_content,
        agent_id=",".join(agent_ids),
        metadata={"agents_used": agent_ids}
    )
    db.add(assistant_message)

    # Update chat title if it's the first message
    if len(chat.messages) <= 1:
        # Generate a title from the first message
        title = message_data.content[:50] + "..." if len(message_data.content) > 50 else message_data.content
        chat.title = title

    chat.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(assistant_message)

    return MessageResponse(
        id=assistant_message.id,
        chat_id=assistant_message.chat_id,
        role=assistant_message.role,
        content=assistant_message.content,
        agent_id=assistant_message.agent_id,
        metadata=assistant_message.metadata or {},
        created_at=assistant_message.created_at
    )


# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict = {}

    async def connect(self, websocket: WebSocket, user_id: int, chat_id: int):
        await websocket.accept()
        key = f"{user_id}:{chat_id}"
        self.active_connections[key] = websocket

    def disconnect(self, user_id: int, chat_id: int):
        key = f"{user_id}:{chat_id}"
        if key in self.active_connections:
            del self.active_connections[key]

    async def send_message(self, message: str, user_id: int, chat_id: int):
        key = f"{user_id}:{chat_id}"
        if key in self.active_connections:
            await self.active_connections[key].send_text(message)


manager = ConnectionManager()


@router.websocket("/ws/{chat_id}")
async def websocket_chat(
    websocket: WebSocket,
    chat_id: int,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db)
):
    """WebSocket endpoint for real-time chat with streaming responses."""
    from jose import jwt, JWTError
    from config import settings
    from sqlalchemy import select

    # Authenticate
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username = payload.get("sub")
        if not username:
            await websocket.close(code=4001)
            return
    except JWTError:
        await websocket.close(code=4001)
        return

    # Get user
    result = await db.execute(select(UserModel).where(UserModel.username == username))
    user = result.scalar_one_or_none()
    if not user:
        await websocket.close(code=4001)
        return

    # Get chat
    result = await db.execute(
        select(ChatModel)
        .options(selectinload(ChatModel.messages))
        .where(ChatModel.id == chat_id)
        .where(ChatModel.user_id == user.id)
    )
    chat = result.scalar_one_or_none()
    if not chat:
        await websocket.close(code=4004)
        return

    await manager.connect(websocket, user.id, chat_id)

    try:
        while True:
            data = await websocket.receive_text()
            message_data = json.loads(data)

            content = message_data.get("content", "")
            agent_ids = message_data.get("agent_ids", chat.agent_ids or [])

            # Save user message
            user_message = MessageModel(
                chat_id=chat_id,
                role="user",
                content=content,
                metadata={}
            )
            db.add(user_message)
            await db.commit()

            # Send acknowledgment
            await websocket.send_text(json.dumps({
                "type": "user_message_saved",
                "message_id": user_message.id
            }))

            # Route if no agents specified
            agent_manager = ForestryAgentManager()
            if not agent_ids:
                routing = await agent_manager.route_message(content)
                agent_ids = routing.get("recommended_agents", ["data_readiness", "qa_qc", "operational_feasibility"])

            # Send routing info
            await websocket.send_text(json.dumps({
                "type": "routing",
                "agents": agent_ids
            }))

            # Get chat history
            result = await db.execute(
                select(ChatModel)
                .options(selectinload(ChatModel.messages))
                .where(ChatModel.id == chat_id)
            )
            chat = result.scalar_one_or_none()
            chat_history = [
                {"role": msg.role, "content": msg.content}
                for msg in sorted(chat.messages, key=lambda x: x.created_at)
            ]

            # Stream response
            full_response = ""
            await websocket.send_text(json.dumps({"type": "stream_start"}))

            async for chunk in agent_manager.run_agent_stream(agent_ids, content, chat_history):
                full_response += chunk
                await websocket.send_text(json.dumps({
                    "type": "stream",
                    "content": chunk
                }))

            await websocket.send_text(json.dumps({"type": "stream_end"}))

            # Save assistant message
            assistant_message = MessageModel(
                chat_id=chat_id,
                role="assistant",
                content=full_response,
                agent_id=",".join(agent_ids),
                metadata={"agents_used": agent_ids}
            )
            db.add(assistant_message)

            # Update chat
            if chat.title == "New Chat":
                chat.title = content[:50] + "..." if len(content) > 50 else content
            chat.agent_ids = agent_ids
            chat.updated_at = datetime.utcnow()
            await db.commit()

            # Send final message
            await websocket.send_text(json.dumps({
                "type": "message_complete",
                "message_id": assistant_message.id,
                "content": full_response,
                "agents": agent_ids
            }))

    except WebSocketDisconnect:
        manager.disconnect(user.id, chat_id)
    except Exception as e:
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": str(e)
        }))
        manager.disconnect(user.id, chat_id)
