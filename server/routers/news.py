"""
news.py — Router para /api/news
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Query, HTTPException

from server.schemas import NewsListResponse, ArticleDetail
from server.services import news_service

router = APIRouter(prefix="/api/news", tags=["news"])


@router.get("", response_model=NewsListResponse)
def list_news(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    source: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
):
    return news_service.list_articles(
        page=page, page_size=page_size, source=source, category=category, q=q
    )


@router.get("/{filename}", response_model=ArticleDetail)
def get_news(filename: str):
    art = news_service.get_article(filename)
    if art is None:
        raise HTTPException(status_code=404, detail="Artículo no encontrado")
    return art