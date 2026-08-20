"""
briefs.py — Router para /api/briefs
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Query, HTTPException

from server.schemas import BriefListResponse, BriefDetail
from server.services import brief_service

router = APIRouter(prefix="/api/briefs", tags=["briefs"])


@router.get("", response_model=BriefListResponse)
def list_briefs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    type: Optional[str] = None,
):
    return brief_service.list_briefs(page=page, page_size=page_size, type=type)


@router.get("/{filename}", response_model=BriefDetail)
def get_brief(filename: str):
    brief = brief_service.get_brief(filename)
    if brief is None:
        raise HTTPException(status_code=404, detail="Brief no encontrado")
    return brief