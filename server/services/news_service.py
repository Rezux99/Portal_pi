"""
news_service.py — Lógica de negocio para noticias.
Lee artículos del disco (raw_news/) y de la BD.
Soporta modo Supabase (consultas DB + Storage) y filesystem (local).
"""

from __future__ import annotations
import os
from pathlib import Path
from typing import List, Optional, Tuple

from scripts.paths import RAW_DIR
from scripts.supabase_client import use_supabase
from server.deps import get_db
from server.schemas import ArticleMeta, ArticleDetail, NewsListResponse


def _parse_raw_file(filepath: Path) -> dict:
    """Parsea un archivo .txt de raw_news y devuelve un dict con metadatos + body."""
    meta = {
        "filename": filepath.name,
        "title": filepath.stem.replace("_", " "),
        "source": "",
        "category": "",
        "link": "",
        "link_type": "",
        "published": "",
        "ingested_at": "",
        "body": "",
    }
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return meta

    lines = text.split("\n")
    body_start = 0
    for i, line in enumerate(lines):
        if line.startswith("FUENTE:"):
            meta["source"] = line.split(":", 1)[1].strip()
        elif line.startswith("CATEGOR"):
            meta["category"] = line.split(":", 1)[1].strip()
        elif line.startswith("T"):
            if line.startswith("TÍTULO:") or line.startswith("TITULO:"):
                meta["title"] = line.split(":", 1)[1].strip()
        elif line.startswith("ENLACE:"):
            meta["link"] = line.split(":", 1)[1].strip()
        elif line.startswith("TIPO_ENLACE:"):
            meta["link_type"] = line.split(":", 1)[1].strip()
        elif line.startswith("FECHA_PUBLICACIÓN:") or line.startswith("FECHA_PUBLICACION:"):
            meta["published"] = line.split(":", 1)[1].strip()
        elif line.startswith("FECHA_INGESTA:"):
            meta["ingested_at"] = line.split(":", 1)[1].strip()
        elif line.strip() == "":
            if i > 2:
                body_start = i + 1
                break

    meta["body"] = "\n".join(lines[body_start:]).strip()
    return meta


def _row_to_article_meta(row: dict) -> ArticleMeta:
    """Convierte una fila de Supabase raw_news en ArticleMeta."""
    snippet = (row.get("content") or "")[:120]
    if len(snippet) >= 120:
        snippet += "..."
    return ArticleMeta(
        filename=row.get("filename", ""),
        title=row.get("title", "") or row.get("filename", "").replace("_", " "),
        source=row.get("source", ""),
        category=row.get("category", ""),
        link=row.get("link", ""),
        link_type=row.get("link_type", ""),
        published=row.get("published", ""),
        ingested_at=str(row.get("ingested_at", "")),
        snippet=snippet,
    )


def list_articles(
    page: int = 1,
    page_size: int = 20,
    source: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
) -> NewsListResponse:
    """Lista artículos paginados con filtros opcionales."""

    # ── Modo Supabase ──
    if use_supabase():
        db = get_db()
        query = db._client.table("raw_news").select("*", count="exact")

        if source:
            query = query.eq("source", source)
        if category:
            query = query.eq("category", category)
        if q:
            query = query.ilike("title", f"%{q}%")

        # Contar total
        count_result = db._client.table("raw_news").select("id", count="exact").execute()
        total = count_result.count if hasattr(count_result, "count") and count_result.count else 0

        # Filtros para total real
        if source or category or q:
            count_q = db._client.table("raw_news").select("id", count="exact")
            if source:
                count_q = count_q.eq("source", source)
            if category:
                count_q = count_q.eq("category", category)
            if q:
                count_q = count_q.ilike("title", f"%{q}%")
            count_result = count_q.execute()
            total = count_result.count if hasattr(count_result, "count") and count_result.count else 0

        # Paginación
        start = (page - 1) * page_size
        end = start + page_size - 1
        result = query.order("ingested_at", desc=True).range(start, end).execute()

        items = [_row_to_article_meta(r) for r in result.data]

        # Fuentes y categorías únicas
        sources_result = db._client.table("raw_news").select("source").execute()
        sources = sorted(set(r["source"] for r in sources_result.data if r.get("source")))
        cats_result = db._client.table("raw_news").select("category").execute()
        categories = sorted(set(r["category"] for r in cats_result.data if r.get("category")))

        return NewsListResponse(
            items=items, total=total, page=page, page_size=page_size,
            sources=sources, categories=categories,
        )

    # ── Modo filesystem (local) ──
    raw_path = Path(RAW_DIR)
    if not raw_path.exists():
        return NewsListResponse(items=[], total=0, page=page, page_size=page_size, sources=[], categories=[])

    all_files = sorted(raw_path.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)

    articles: List[dict] = []
    sources_set = set()
    categories_set = set()

    for f in all_files:
        art = _parse_raw_file(f)
        if art["source"]:
            sources_set.add(art["source"])
        if art["category"]:
            categories_set.add(art["category"])

        if source and art["source"] != source:
            continue
        if category and art["category"] != category:
            continue
        if q and q.lower() not in art["title"].lower() and q.lower() not in art["body"].lower():
            continue

        art["snippet"] = art["body"][:120] + ("..." if len(art["body"]) > 120 else "")
        articles.append(art)

    total = len(articles)
    start = (page - 1) * page_size
    end = start + page_size
    page_items = articles[start:end]

    items = [
        ArticleMeta(
            filename=a["filename"], title=a["title"], source=a["source"],
            category=a["category"], link=a["link"], link_type=a["link_type"],
            published=a["published"], ingested_at=a["ingested_at"], snippet=a["snippet"],
        )
        for a in page_items
    ]

    return NewsListResponse(
        items=items, total=total, page=page, page_size=page_size,
        sources=sorted(sources_set), categories=sorted(categories_set),
    )


def get_article(filename: str) -> Optional[ArticleDetail]:
    """Devuelve un artículo completo por nombre de archivo."""

    # ── Modo Supabase ──
    if use_supabase():
        db = get_db()
        result = db._client.table("raw_news").select("*").eq("filename", filename).execute()
        if result.data:
            row = result.data[0]
            return ArticleDetail(
                filename=row.get("filename", ""),
                title=row.get("title", "") or row.get("filename", "").replace("_", " "),
                source=row.get("source", ""),
                category=row.get("category", ""),
                link=row.get("link", ""),
                link_type=row.get("link_type", ""),
                published=row.get("published", ""),
                ingested_at=str(row.get("ingested_at", "")),
                body=row.get("content", ""),
                snippet=(row.get("content") or "")[:120] + "..." if len(row.get("content") or "") > 120 else row.get("content", ""),
            )
        return None

    # ── Modo filesystem ──
    filepath = Path(RAW_DIR) / filename
    if not filepath.exists():
        return None
    art = _parse_raw_file(filepath)
    return ArticleDetail(**art)
