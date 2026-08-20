"""
chat.py — Router para /api/chat (El Analista)
Soporta auth opcional (per-user chat en Supabase).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from server.schemas import ChatRequest, ChatResponse
from server.services import chat_service
from server.auth import optional_auth

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
def post_chat(request: ChatRequest, user: dict = Depends(optional_auth)):
    return chat_service.chat(request, user=user)
