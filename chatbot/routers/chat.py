from fastapi import APIRouter, BackgroundTasks

from chatbot.core import redis_client
from chatbot.services.chat_service import ChatRequest, ChatResponse
from chatbot.services.chat_service import chat_handler as _chat_handler

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, background_tasks: BackgroundTasks):
    return await _chat_handler(request, background_tasks)


@router.delete("/chat/{session_id}")
async def clear_session(session_id: str):
    await redis_client.client.delete(f"koolbuy:chat:{session_id}")
    return {"session_id": session_id, "message": "Session cleared."}


@router.get("/chat/{session_id}/history")
async def get_chat_history(session_id: str):
    history = await redis_client.get_history(session_id)
    return {"session_id": session_id, "history": history, "count": len(history)}
