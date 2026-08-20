"""
supabase_storage.py — Backends de almacenamiento para Portal Pi.
- SupabaseStorage: usa Supabase Storage buckets (persiste en la nube)
- FilesystemStorage: usa disco local (modo dev/fallback)
- get_storage(): factory que devuelve el backend apropiado
"""

import json
from pathlib import Path
from typing import List, Optional, Dict, Any

from scripts.supabase_client import get_supabase_admin, use_supabase
from scripts.paths import (
    RAW_DIR, SYNTHESIZED_DIR, REPORTS_DIR, TIMELINE_DIR, ENTITIES_TIMELINE_DIR,
)


class SupabaseStorage:
    """Almacenamiento en Supabase Storage buckets."""

    def __init__(self) -> None:
        self._client = get_supabase_admin()
        if self._client is None:
            raise RuntimeError("Supabase no configurado.")

    # ─── RAW NEWS ────────────────────────────────────────────────────────

    def save_raw_news(self, filename: str, content: str) -> None:
        self._client.storage.from_("raw-news").upload(filename, content.encode("utf-8"), {"content-type": "text/plain; charset=utf-8", "upsert": "true"})

    def read_raw_news(self, filename: str) -> Optional[str]:
        try:
            data = self._client.storage.from_("raw-news").download(filename)
            return data.decode("utf-8")
        except Exception:
            return None

    def list_raw_news(self) -> List[str]:
        try:
            files = self._client.storage.from_("raw-news").list()
            return [f["name"] for f in files if not f.get("id", "").startswith(".")]
        except Exception:
            return []

    # ─── PIPELINE OUTPUTS ────────────────────────────────────────────────

    def save_pipeline_output(self, path: str, content: str) -> None:
        self._client.storage.from_("pipeline-outputs").upload(path, content.encode("utf-8"), {"content-type": "application/json", "upsert": "true"})

    def read_pipeline_output(self, path: str) -> Optional[str]:
        try:
            data = self._client.storage.from_("pipeline-outputs").download(path)
            return data.decode("utf-8")
        except Exception:
            return None

    def list_pipeline_outputs(self, prefix: str = "") -> List[str]:
        try:
            files = self._client.storage.from_("pipeline-outputs").list(prefix)
            return [f["name"] for f in files if not f.get("id", "").startswith(".")]
        except Exception:
            return []

    # ─── REPORTS ─────────────────────────────────────────────────────────

    def save_report(self, filename: str, content: str) -> None:
        self._client.storage.from_("reports").upload(filename, content.encode("utf-8"), {"content-type": "text/markdown; charset=utf-8", "upsert": "true"})

    def read_report(self, filename: str) -> Optional[str]:
        try:
            data = self._client.storage.from_("reports").download(filename)
            return data.decode("utf-8")
        except Exception:
            return None

    def list_reports(self) -> List[str]:
        try:
            files = self._client.storage.from_("reports").list()
            return [f["name"] for f in files if not f.get("id", "").startswith(".")]
        except Exception:
            return []

    # ─── TIMELINE ────────────────────────────────────────────────────────

    def save_timeline(self, path: str, content: str) -> None:
        self._client.storage.from_("timeline").upload(path, content.encode("utf-8"), {"content-type": "text/markdown; charset=utf-8", "upsert": "true"})

    def read_timeline(self, path: str) -> Optional[str]:
        try:
            data = self._client.storage.from_("timeline").download(path)
            return data.decode("utf-8")
        except Exception:
            return None

    # ─── ENTITY PROFILES ─────────────────────────────────────────────────

    def save_entity_profile(self, filename: str, content: str) -> None:
        self._client.storage.from_("timeline").upload(f"entities/{filename}", content.encode("utf-8"), {"content-type": "text/markdown; charset=utf-8", "upsert": "true"})

    def read_entity_profile(self, filename: str) -> Optional[str]:
        try:
            data = self._client.storage.from_("timeline").download(f"entities/{filename}")
            return data.decode("utf-8")
        except Exception:
            return None


class FilesystemStorage:
    """Almacenamiento en disco local — modo dev / fallback."""

    def save_raw_news(self, filename: str, content: str) -> None:
        path = RAW_DIR / filename
        path.write_text(content, encoding="utf-8")

    def read_raw_news(self, filename: str) -> Optional[str]:
        path = RAW_DIR / filename
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return None

    def list_raw_news(self) -> List[str]:
        if RAW_DIR.exists():
            return [f.name for f in RAW_DIR.glob("*.txt")]
        return []

    def save_pipeline_output(self, path: str, content: str) -> None:
        # path puede ser como "entities/entities_20260729.json"
        full_path = SYNTHESIZED_DIR.parent / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

    def read_pipeline_output(self, path: str) -> Optional[str]:
        full_path = SYNTHESIZED_DIR.parent / path
        if full_path.exists():
            return full_path.read_text(encoding="utf-8", errors="replace")
        return None

    def list_pipeline_outputs(self, prefix: str = "") -> List[str]:
        # Buscar en todos los dirs de pipeline
        results = []
        for d in [SYNTHESIZED_DIR, Path(SYNTHESIZED_DIR.parent / "entities"),
                  Path(SYNTHESIZED_DIR.parent / "classified"),
                  Path(SYNTHESIZED_DIR.parent / "action_items")]:
            if d.exists():
                for f in d.glob("*.json"):
                    results.append(f.name)
        return results

    def save_report(self, filename: str, content: str) -> None:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORTS_DIR / filename
        path.write_text(content, encoding="utf-8")

    def read_report(self, filename: str) -> Optional[str]:
        path = REPORTS_DIR / filename
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return None

    def list_reports(self) -> List[str]:
        if REPORTS_DIR.exists():
            return [f.name for f in REPORTS_DIR.glob("*.md")]
        return []

    def save_timeline(self, path: str, content: str) -> None:
        full_path = TIMELINE_DIR / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

    def read_timeline(self, path: str) -> Optional[str]:
        full_path = TIMELINE_DIR / path
        if full_path.exists():
            return full_path.read_text(encoding="utf-8", errors="replace")
        return None

    def save_entity_profile(self, filename: str, content: str) -> None:
        ENTITIES_TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
        path = ENTITIES_TIMELINE_DIR / filename
        path.write_text(content, encoding="utf-8")

    def read_entity_profile(self, filename: str) -> Optional[str]:
        path = ENTITIES_TIMELINE_DIR / filename
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return None


def get_storage():
    """Factory: devuelve SupabaseStorage o FilesystemStorage según config."""
    if use_supabase():
        try:
            return SupabaseStorage()
        except Exception:
            pass
    return FilesystemStorage()
