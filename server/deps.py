"""
deps.py — Singletons lazy para los servicios del backend.
Evita instanciar múltiples veces DB, Ingester, Storage, etc.
Auto-detecta Supabase via environment variables.
"""

from __future__ import annotations
from typing import Optional, Union

from scripts.paths import DB_PATH, RAW_DIR, SYNTHESIZED_DIR, REPORTS_DIR, FEEDS_CONFIG_PATH
from scripts.database import PortalDatabase
from scripts.ingester import FeedIngester
from scripts.supabase_client import use_supabase
from scripts.supabase_storage import get_storage, SupabaseStorage, FilesystemStorage


_db: Optional[Union[PortalDatabase, "SupabaseDatabase"]] = None
_ingester: Optional[FeedIngester] = None
_storage: Optional[Union[SupabaseStorage, FilesystemStorage]] = None


def get_db() -> Union[PortalDatabase, "SupabaseDatabase"]:
    global _db
    if _db is None:
        if use_supabase():
            from scripts.supabase_database import SupabaseDatabase
            _db = SupabaseDatabase()
        else:
            _db = PortalDatabase(str(DB_PATH))
    return _db


def get_ingester() -> FeedIngester:
    global _ingester
    if _ingester is None:
        _ingester = FeedIngester()
    return _ingester


def get_storage_backend() -> Union[SupabaseStorage, FilesystemStorage]:
    """Devuelve el backend de storage apropiado."""
    global _storage
    if _storage is None:
        _storage = get_storage()
    return _storage
