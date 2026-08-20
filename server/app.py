"""
app.py — Aplicación FastAPI principal de Portal Pi v2.
Monta routers, static files y middleware.
"""

from __future__ import annotations
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from server.routers import news, briefs, chat, ops, market
from server.routers.auth_router import router as auth_router
from scripts.supabase_client import use_supabase

# ─── Scheduler singleton ─────────────────────────────────────────────────

_scheduler = None


def get_scheduler():
    global _scheduler
    if _scheduler is None:
        from scripts.scheduler import PipelineScheduler
        _scheduler = PipelineScheduler()
    return _scheduler


# ─── App factory ──────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Portal Pi v2",
        description="Inteligencia periodística — API",
        version="2.1.0",
    )

    # ─── Startup: arrancar scheduler si está habilitado ────────────────
    @app.on_event("startup")
    def _on_startup():
        sched = get_scheduler()
        settings = sched.get_settings()
        if settings.get("enabled"):
            sched.start()
            print(f"[Portal Pi] Scheduler iniciado — ingesta cada {settings.get('ingest_interval_min', 30)} min, auto_pipeline={settings.get('auto_pipeline', True)}")
        else:
            print("[Portal Pi] Scheduler deshabilitado en configuración")

    @app.on_event("shutdown")
    def _on_shutdown():
        sched = get_scheduler()
        if sched.is_running:
            sched.stop()
            print("[Portal Pi] Scheduler detenido")

    # CORS (dev-friendly)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ─── Routers ───────────────────────────────────────────────────────
    app.include_router(auth_router)
    app.include_router(news.router)
    app.include_router(briefs.router)
    app.include_router(chat.router)
    app.include_router(ops.router)
    app.include_router(market.router)

    # ─── Health check ──────────────────────────────────────────────────
    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "2.1.0", "supabase": use_supabase()}

    # ─── Frontend (Fase 2) ──────────────────────────────────────────────
    from fastapi.responses import FileResponse

    templates_dir = Path(__file__).resolve().parent.parent / "templates"

    @app.get("/")
    def serve_index():
        return FileResponse(templates_dir / "index.html")

    # Mount static assets if they exist
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()
