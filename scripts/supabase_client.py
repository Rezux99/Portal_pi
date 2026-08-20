"""
supabase_client.py — Singleton clients para Supabase.
- get_supabase(): cliente anon (para auth del lado cliente)
- get_supabase_admin(): cliente service_role (bypassea RLS, para backend)
- use_supabase(): feature detection — True si SUPABASE_URL está configurada
"""

import os
from typing import Optional


_client: Optional[object] = None
_admin_client: Optional[object] = None


def use_supabase() -> bool:
    return bool(os.environ.get("SUPABASE_URL"))


def get_supabase():
    """Cliente con anon key — para auth y operaciones con RLS."""
    global _client
    if _client is None and use_supabase():
        anon_key = os.environ.get("SUPABASE_KEY")
        if not anon_key:
            return None
        from supabase import create_client
        _client = create_client(
            os.environ["SUPABASE_URL"],
            anon_key,
        )
    return _client


def get_supabase_admin():
    """Cliente con service_role key — bypassea RLS. Solo para backend."""
    global _admin_client
    if _admin_client is None and use_supabase():
        service_key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not service_key:
            return None
        from supabase import create_client
        _admin_client = create_client(
            os.environ["SUPABASE_URL"],
            service_key,
        )
    return _admin_client


def reset_clients():
    """Resetea singletons (útil para tests)."""
    global _client, _admin_client
    _client = None
    _admin_client = None
