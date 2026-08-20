"""
ops_service.py — Lógica de negocio para Operaciones.
Ingesta, estado del sistema, gestión de feeds.
Soporta modo Supabase (DB + Storage) y filesystem (local).
"""

from __future__ import annotations
import time
import json
from pathlib import Path
from typing import List, Optional, Dict, Any

from scripts.paths import RAW_DIR, SYNTHESIZED_DIR, REPORTS_DIR, DB_PATH, FEEDS_CONFIG_PATH
from scripts.supabase_client import use_supabase
from server.deps import get_db, get_ingester
from server.schemas import SystemStatus, FeedInfo, JobStatus, ActionResponse


_start_time = time.time()


def get_system_status() -> SystemStatus:
    """Devuelve el estado actual del sistema."""

    # ── Modo Supabase ──
    if use_supabase():
        db = get_db()
        try:
            stats = db.stats()
            raw_count = stats.get("raw_news", 0)

            # Feed configs desde DB
            feeds = db.list_feed_configs()
            feeds_total = len(feeds)
            feeds_enabled = sum(1 for f in feeds if f.get("enabled", True))

            # Última ingesta
            last_ingest = None
            try:
                result = db._client.table("raw_news").select("ingested_at").order("ingested_at", desc=True).limit(1).execute()
                if result.data:
                    last_ingest = str(result.data[0]["ingested_at"])
            except Exception:
                pass

            return SystemStatus(
                status="ok",
                db_stats=stats,
                feeds_total=feeds_total,
                feeds_enabled=feeds_enabled,
                raw_articles_on_disk=raw_count,
                last_ingest=last_ingest,
                uptime_sec=round(time.time() - _start_time, 1),
            )
        except Exception:
            return SystemStatus(status="ok", uptime_sec=round(time.time() - _start_time, 1))

    # ── Modo filesystem ──
    db = get_db()
    raw_count = 0
    raw_path = Path(RAW_DIR)
    if raw_path.exists():
        raw_count = len(list(raw_path.glob("*.txt")))

    feeds_total = 0
    feeds_enabled = 0
    try:
        raw = Path(FEEDS_CONFIG_PATH).read_text(encoding="utf-8")
        config = json.loads(raw)
        feeds_list = config.get("feeds", [])
        feeds_total = len(feeds_list)
        feeds_enabled = sum(1 for f in feeds_list if f.get("enabled", True))
    except Exception:
        pass

    db_stats = {}
    try:
        if hasattr(db, "conn"):
            cur = db.conn.execute("SELECT COUNT(*) FROM articles")
            db_stats["articles_db"] = cur.fetchone()[0]
    except Exception:
        pass

    last_ingest = None
    try:
        if hasattr(db, "conn"):
            cur = db.conn.execute("SELECT MAX(ingested_at) FROM articles")
            row = cur.fetchone()
            if row and row[0]:
                last_ingest = row[0]
    except Exception:
        pass

    return SystemStatus(
        status="ok",
        db_stats=db_stats,
        feeds_total=feeds_total,
        feeds_enabled=feeds_enabled,
        raw_articles_on_disk=raw_count,
        last_ingest=last_ingest,
        uptime_sec=round(time.time() - _start_time, 1),
    )


def list_feeds() -> List[FeedInfo]:
    """Lista todos los feeds configurados."""

    # ── Modo Supabase ──
    if use_supabase():
        db = get_db()
        feeds = db.list_feed_configs()
        return [
            FeedInfo(
                name=f.get("name", ""),
                url=f.get("url", ""),
                category=f.get("category", "Otro"),
                enabled=f.get("enabled", True),
                poll_interval_min=f.get("poll_interval_min", 30),
            )
            for f in feeds
        ]

    # ── Modo filesystem ──
    try:
        raw = Path(FEEDS_CONFIG_PATH).read_text(encoding="utf-8")
        config = json.loads(raw)
        feeds = config.get("feeds", [])
        return [
            FeedInfo(
                name=f.get("name", ""),
                url=f.get("url", ""),
                category=f.get("category", "Otro"),
                enabled=f.get("enabled", True),
                poll_interval_min=f.get("poll_interval_min", 30),
            )
            for f in feeds
        ]
    except Exception:
        return []


def add_feed(name: str, url: str, category: str = "Otro", poll_interval_min: int = 30) -> ActionResponse:
    """Añade un nuevo feed."""

    # ── Modo Supabase ──
    if use_supabase():
        db = get_db()
        # Verificar duplicado
        existing = db._client.table("feed_configs").select("id").eq("url", url).execute()
        if existing.data:
            return ActionResponse(ok=False, message="Ya existe un feed con esa URL")
        db.upsert_feed_config(name, url, category, True, poll_interval_min)
        return ActionResponse(ok=True, message=f"Feed '{name}' añadido correctamente")

    # ── Modo filesystem ──
    try:
        raw = Path(FEEDS_CONFIG_PATH).read_text(encoding="utf-8")
        config = json.loads(raw)
    except Exception:
        config = {"feeds": [], "settings": {}}

    feeds = config.get("feeds", [])
    for f in feeds:
        if f.get("url") == url:
            return ActionResponse(ok=False, message="Ya existe un feed con esa URL")

    feeds.append({
        "name": name, "url": url, "category": category,
        "enabled": True, "poll_interval_min": poll_interval_min,
    })
    config["feeds"] = feeds
    Path(FEEDS_CONFIG_PATH).write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return ActionResponse(ok=True, message=f"Feed '{name}' añadido correctamente")


def run_ingest() -> ActionResponse:
    """Ejecuta una ingesta manual de todos los feeds."""
    try:
        ingester = get_ingester()
        ingester.ingest_all()
        return ActionResponse(ok=True, message="Ingesta completada")
    except Exception as e:
        return ActionResponse(ok=False, message=f"Error en ingesta: {e}")


def run_pipeline() -> ActionResponse:
    """Ejecuta el pipeline completo: ingesta → extract → classify → synthesize → action items."""
    try:
        from scripts.llm_client import LLMClient
        from scripts.main import run_simple_pipeline
        from scripts.state_manager import StateManager
        from scripts.paths import STATE_PATH

        ingester = get_ingester()
        ingester.ingest_all()

        llm = LLMClient()
        state_mgr = StateManager(str(STATE_PATH))
        results = run_simple_pipeline(llm, state_mgr)

        ok_count = sum(1 for r in results if r["status"] == "ok")
        skip_count = sum(1 for r in results if r["status"] == "skipped")
        err_count = sum(1 for r in results if r["status"] == "error")
        return ActionResponse(
            ok=True,
            message=f"Pipeline completado: {ok_count} OK, {skip_count} skipped, {err_count} errores"
        )
    except Exception as e:
        return ActionResponse(ok=False, message=f"Error en pipeline: {e}")
