"""
auth_router.py — Router para /api/auth (login, register, refresh, me).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server.auth import (
    LoginRequest, RegisterRequest, AuthResponse, RefreshRequest,
    require_auth, USE_SUPABASE,
)
from scripts.supabase_client import get_supabase


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=AuthResponse)
def login(req: LoginRequest):
    if not USE_SUPABASE:
        return AuthResponse(
            access_token="local-mode-no-auth",
            refresh_token="",
            user={"id": "local-user", "email": req.email},
        )

    client = get_supabase()
    if client is None:
        raise HTTPException(status_code=500, detail="Supabase no configurado")

    try:
        result = client.auth.sign_in_with_password({"email": req.email, "password": req.password})
        return AuthResponse(
            access_token=result.session.access_token,
            refresh_token=result.session.refresh_token,
            user={"id": result.user.id, "email": result.user.email},
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Login fallido: {exc}")


@router.post("/register", response_model=AuthResponse)
def register(req: RegisterRequest):
    if not USE_SUPABASE:
        return AuthResponse(
            access_token="local-mode-no-auth",
            refresh_token="",
            user={"id": "local-user", "email": req.email},
        )

    client = get_supabase()
    if client is None:
        raise HTTPException(status_code=500, detail="Supabase no configurado")

    try:
        meta = {}
        if req.display_name:
            meta["display_name"] = req.display_name
        result = client.auth.sign_up({
            "email": req.email,
            "password": req.password,
            "options": {"data": meta},
        })
        token = ""
        refresh = ""
        if result.session:
            token = result.session.access_token
            refresh = result.session.refresh_token
        return AuthResponse(
            access_token=token,
            refresh_token=refresh,
            user={"id": result.user.id, "email": result.user.email},
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Registro fallido: {exc}")


@router.post("/refresh", response_model=AuthResponse)
def refresh(req: RefreshRequest):
    if not USE_SUPABASE:
        return AuthResponse(access_token="local-mode-no-auth", refresh_token="")

    client = get_supabase()
    if client is None:
        raise HTTPException(status_code=500, detail="Supabase no configurado")

    try:
        result = client.auth.refresh_session(req.refresh_token)
        return AuthResponse(
            access_token=result.session.access_token,
            refresh_token=result.session.refresh_token,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Refresh fallido: {exc}")


@router.get("/me")
def get_me(user: dict = require_auth):
    return user


@router.get("/mode")
def auth_mode():
    """Devuelve si el sistema requiere autenticación o está en modo local."""
    return {"supabase": USE_SUPABASE, "auth_required": USE_SUPABASE}
