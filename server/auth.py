"""
auth.py — Autenticación JWT para Portal Pi.
Valida tokens de Supabase Auth o permite modo local sin auth.
"""

import os
from typing import Optional, Dict, Any

from fastapi import Depends, HTTPException, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from pydantic import BaseModel

from scripts.supabase_client import use_supabase, get_supabase


USE_SUPABASE = use_supabase()

security = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = ""


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str = ""
    user: Optional[Dict[str, Any]] = None


class RefreshRequest(BaseModel):
    refresh_token: str


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Dict[str, Any]:
    """
    Dependencia FastAPI: valida JWT y devuelve el dict de usuario.
    En modo local, permite acceso sin token.
    """
    if not USE_SUPABASE:
        return {"id": "local-user", "email": "local@portalpi.dev", "role": "admin"}

    if credentials is None:
        raise HTTPException(status_code=401, detail="Token de autenticación requerido")

    token = credentials.credentials
    try:
        client = get_supabase()
        if client is None:
            raise HTTPException(status_code=500, detail="Supabase no configurado")

        user = client.auth.get_user(token)
        if user and user.user:
            return {
                "id": user.user.id,
                "email": user.user.email or "",
                "role": "authenticated",
            }
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Token inválido: {exc}")

    raise HTTPException(status_code=401, detail="Token inválido")


async def optional_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[Dict[str, Any]]:
    """Como require_auth pero devuelve None en vez de 401."""
    if not USE_SUPABASE:
        return {"id": "local-user", "email": "local@portalpi.dev", "role": "admin"}

    if credentials is None:
        return None

    try:
        client = get_supabase()
        if client is None:
            return None
        user = client.auth.get_user(credentials.credentials)
        if user and user.user:
            return {
                "id": user.user.id,
                "email": user.user.email or "",
                "role": "authenticated",
            }
    except Exception:
        pass

    return None
