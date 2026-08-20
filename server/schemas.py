"""
schemas.py — Modelos Pydantic para la API REST de Portal Pi v2.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


# ─── News ────────────────────────────────────────────────────────────────

class ArticleMeta(BaseModel):
    """Artículo individual en la lista de noticias."""
    filename: str
    title: str
    source: str
    category: str
    link: str = ""
    link_type: str = ""
    published: str = ""
    ingested_at: str = ""
    snippet: str = Field("", description="Primeras ~120 chars del cuerpo")


class ArticleDetail(ArticleMeta):
    """Artículo completo con cuerpo."""
    body: str = ""


class NewsListResponse(BaseModel):
    items: List[ArticleMeta]
    total: int
    page: int
    page_size: int
    sources: List[str] = Field(default_factory=list, description="Fuentes únicas disponibles")
    categories: List[str] = Field(default_factory=list, description="Categorías únicas disponibles")


# ─── Briefs ──────────────────────────────────────────────────────────────

class BriefMeta(BaseModel):
    filename: str
    title: str
    created_at: str = ""
    type: str = Field("synthesis", description="synthesis | report | orchestrated")
    snippet: str = ""


class BriefDetail(BriefMeta):
    body: str = ""


class BriefListResponse(BaseModel):
    items: List[BriefMeta]
    total: int
    page: int
    page_size: int


class BriefGenerateRequest(BaseModel):
    type: str = Field("synthesis", description="synthesis | report")
    sources: Optional[List[str]] = None


# ─── Chat ────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    context_files: Optional[List[str]] = None


class ChatResponse(BaseModel):
    reply: str
    sources: List[str] = Field(default_factory=list)


# ─── Ops ─────────────────────────────────────────────────────────────────

class SystemStatus(BaseModel):
    status: str = "ok"
    db_stats: Dict[str, int] = Field(default_factory=dict)
    feeds_total: int = 0
    feeds_enabled: int = 0
    raw_articles_on_disk: int = 0
    last_ingest: Optional[str] = None
    uptime_sec: float = 0.0


class JobStatus(BaseModel):
    job_id: str
    job_type: str
    status: str  # pending | running | done | error
    progress: float = 0.0
    result: Optional[Any] = None
    error: Optional[str] = None


class FeedInfo(BaseModel):
    name: str
    url: str
    category: str = "Otro"
    enabled: bool = True
    poll_interval_min: int = 30


class FeedAddRequest(BaseModel):
    name: str
    url: str
    category: str = "Otro"
    poll_interval_min: int = 30


class ActionResponse(BaseModel):
    ok: bool
    message: str = ""
