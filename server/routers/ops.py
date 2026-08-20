"""
ops.py — Router para /api/ops (Operaciones)
Soporta credenciales per-user en modo Supabase.
"""

from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server.schemas import SystemStatus, FeedInfo, FeedAddRequest, ActionResponse
from server.services import ops_service
from server.auth import optional_auth, USE_SUPABASE

router = APIRouter(prefix="/api/ops", tags=["ops"])


# ─── Schemas locales ────────────────────────────────────────────────────

class CredentialUpdate(BaseModel):
    provider: str
    api_key: str


class CredentialInfo(BaseModel):
    provider: str
    has_key: bool


# ─── Endpoints ───────────────────────────────────────────────────────────

@router.get("/status", response_model=SystemStatus)
def get_status():
    return ops_service.get_system_status()


@router.get("/feeds", response_model=List[FeedInfo])
def list_feeds():
    return ops_service.list_feeds()


@router.post("/feeds", response_model=ActionResponse)
def add_feed(req: FeedAddRequest):
    return ops_service.add_feed(
        name=req.name, url=req.url,
        category=req.category, poll_interval_min=req.poll_interval_min
    )


@router.post("/ingest", response_model=ActionResponse)
def trigger_ingest():
    return ops_service.run_ingest()


@router.post("/pipeline", response_model=ActionResponse)
def trigger_pipeline():
    return ops_service.run_pipeline()


# ─── Credenciales LLM ───────────────────────────────────────────────────

def _list_llm_providers_with_user_status(user: Optional[dict] = None) -> List[CredentialInfo]:
    """Lista proveedores con status, incluyendo claves de usuario en Supabase."""
    from scripts.llm_client import LLMClient, LLMClientError
    try:
        client = LLMClient()
    except LLMClientError:
        return []

    result = []
    user_creds = {}
    if USE_SUPABASE and user and user.get("id") != "local-user":
        try:
            from server.deps import get_db
            db = get_db()
            creds = db.list_user_credentials(user["id"])
            user_creds = {c["provider"]: c["api_key"] for c in creds}
        except Exception:
            pass

    for name in client.fallback_order:
        if name in client.providers:
            has_key = bool(client.get_credential(name)) or bool(user_creds.get(name))
            result.append(CredentialInfo(provider=name, has_key=has_key))
    return result


@router.get("/credentials", response_model=List[CredentialInfo])
def list_credentials(user: dict = Depends(optional_auth)):
    """Lista proveedores LLM y si tienen key configurada."""
    if USE_SUPABASE and user and user.get("id") != "local-user":
        return _list_llm_providers_with_user_status(user)

    # Modo local
    from scripts.llm_client import LLMClient, LLMClientError
    try:
        client = LLMClient()
    except LLMClientError:
        return []
    result = []
    for name in client.fallback_order:
        if name in client.providers:
            key = client.get_credential(name)
            result.append(CredentialInfo(provider=name, has_key=bool(key)))
    return result


@router.post("/credentials", response_model=ActionResponse)
def set_credential(req: CredentialUpdate, user: dict = Depends(optional_auth)):
    """Establece la API key para un proveedor."""
    # En modo Supabase, guardar en user_credentials
    if USE_SUPABASE and user and user.get("id") != "local-user":
        try:
            from server.deps import get_db
            db = get_db()
            db.set_user_credential(user["id"], req.provider, req.api_key)
            return ActionResponse(ok=True, detail=f"Key para '{req.provider}' guardada")
        except Exception as e:
            return ActionResponse(ok=False, detail=str(e))

    # Modo local: guardar en .credentials.json
    from scripts.llm_client import LLMClient, LLMClientError
    try:
        client = LLMClient()
    except LLMClientError as e:
        return ActionResponse(ok=False, detail=str(e))
    client.set_credential(req.provider, req.api_key)
    return ActionResponse(ok=True, detail=f"Key para '{req.provider}' guardada")


@router.delete("/credentials/{provider}", response_model=ActionResponse)
def delete_credential(provider: str, user: dict = Depends(optional_auth)):
    """Elimina la API key de un proveedor."""
    if USE_SUPABASE and user and user.get("id") != "local-user":
        try:
            from server.deps import get_db
            db = get_db()
            db.delete_user_credential(user["id"], provider)
            return ActionResponse(ok=True, detail=f"Key para '{provider}' eliminada")
        except Exception as e:
            return ActionResponse(ok=False, detail=str(e))

    from scripts.llm_client import LLMClient, LLMClientError
    try:
        client = LLMClient()
    except LLMClientError as e:
        return ActionResponse(ok=False, detail=str(e))
    client.set_credential(provider, "")
    return ActionResponse(ok=True, detail=f"Key para '{provider}' eliminada")


# ─── Scheduler ───────────────────────────────────────────────────────────

@router.get("/scheduler")
def scheduler_status():
    from server.app import get_scheduler
    return get_scheduler().status()


@router.post("/scheduler/start", response_model=ActionResponse)
def scheduler_start():
    from server.app import get_scheduler
    result = get_scheduler().start()
    get_scheduler().update_settings({"enabled": True})
    return ActionResponse(ok=True, detail=result.get("message", "Scheduler iniciado"))


@router.post("/scheduler/stop", response_model=ActionResponse)
def scheduler_stop():
    from server.app import get_scheduler
    result = get_scheduler().stop()
    get_scheduler().update_settings({"enabled": False})
    return ActionResponse(ok=True, detail=result.get("message", "Scheduler detenido"))
