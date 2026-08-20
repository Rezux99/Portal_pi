"""
brief_service.py — Lógica de negocio para briefs/síntesis.
Lee archivos de data/synthesized/ y data/reports/.
Soporta modo Supabase (DB queries + Storage) y filesystem (local).
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from scripts.paths import SYNTHESIZED_DIR, REPORTS_DIR
from scripts.supabase_client import use_supabase
from server.deps import get_db, get_storage_backend
from server.schemas import BriefMeta, BriefDetail, BriefListResponse


_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})|(\d{4})(\d{2})(\d{2})")


def _extract_date_from_filename(filename: str) -> str:
    m = _DATE_RE.search(filename)
    if not m:
        return ""
    if m.group(1):
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}"
    return f"{m.group(7)}-{m.group(8)}-{m.group(9)}"


def _parse_synthesis_file(filepath: Path) -> dict:
    data = {
        "filename": filepath.name, "title": "", "created_at": "",
        "type": "synthesis", "snippet": "", "body": "",
    }
    try:
        raw = filepath.read_text(encoding="utf-8", errors="replace")
        if filepath.suffix == ".json":
            obj = json.loads(raw)
            data["title"] = obj.get("title", obj.get("headline", ""))
            data["created_at"] = obj.get("created_at", obj.get("timestamp", ""))
            if not data["created_at"]:
                data["created_at"] = _extract_date_from_filename(filepath.name)
            if not data["title"]:
                if "executive_summary" in obj:
                    data["title"] = obj["executive_summary"][:120]
                else:
                    data["title"] = filepath.stem.replace("_", " ")
            body_parts = []
            for key in ("summary", "analysis", "key_points", "conclusion", "content", "text", "executive_summary"):
                if key in obj and key != "title":
                    val = obj[key]
                    if isinstance(val, list):
                        body_parts.append("\n".join(str(v) for v in val))
                    else:
                        body_parts.append(str(val))
            data["body"] = "\n\n".join(body_parts) if body_parts else raw[:2000]
            data["snippet"] = data["body"][:150] + ("..." if len(data["body"]) > 150 else "")
            name_lower = filepath.name.lower()
            if "orchestrated" in name_lower:
                data["type"] = "orchestrated"
            elif "report" in name_lower:
                data["type"] = "report"
        else:
            data["title"] = filepath.stem.replace("_", " ")
            data["body"] = raw
            data["snippet"] = raw[:150] + ("..." if len(raw) > 150 else "")
            if not data["created_at"]:
                data["created_at"] = _extract_date_from_filename(filepath.name)
    except Exception:
        pass
    return data


def _parse_report_file(filepath: Path) -> dict:
    data = {
        "filename": filepath.name, "title": filepath.stem.replace("_", " "),
        "created_at": "", "type": "report", "snippet": "", "body": "",
    }
    try:
        raw = filepath.read_text(encoding="utf-8", errors="replace")
        data["body"] = raw
        for line in raw.split("\n"):
            if line.startswith("# "):
                data["title"] = line.lstrip("# ").strip()
                break
        data["created_at"] = _extract_date_from_filename(filepath.name)
        data["snippet"] = raw[:150] + ("..." if len(raw) > 150 else "")
    except Exception:
        pass
    return data


def _synthesis_row_to_brief(row: dict) -> dict:
    """Convierte una fila de Supabase syntheses a dict de brief."""
    snippet = (row.get("executive_summary") or "")[:150]
    if len(snippet) >= 150:
        snippet += "..."
    return {
        "filename": row.get("output_filename", f"synthesis_{row.get('id', '')}.json"),
        "title": row.get("executive_summary", "")[:120] or "Síntesis",
        "created_at": str(row.get("created_at", "")),
        "type": "synthesis",
        "snippet": snippet,
        "body": row.get("executive_summary", ""),
    }


def list_briefs(
    page: int = 1,
    page_size: int = 20,
    type: Optional[str] = None,
) -> BriefListResponse:
    """Lista briefs paginados."""

    # ── Modo Supabase ──
    if use_supabase():
        db = get_db()
        storage = get_storage_backend()
        items: List[dict] = []

        # Síntesis desde DB
        if type in (None, "synthesis", "all", "orchestrated"):
            synth_result = db._client.table("syntheses").select("*").order("created_at", desc=True).execute()
            for row in synth_result.data:
                brief = _synthesis_row_to_brief(row)
                if type and brief["type"] != type and type != "all":
                    continue
                items.append(brief)

        # Reports desde Storage
        if type is None or type in ("report", "all"):
            report_files = storage.list_reports()
            for fname in report_files:
                content = storage.read_report(fname)
                if content:
                    brief = {"filename": fname, "title": fname.replace("_", " "), "created_at": "", "type": "report", "snippet": content[:150] + "...", "body": content}
                    for line in content.split("\n"):
                        if line.startswith("# "):
                            brief["title"] = line.lstrip("# ").strip()
                            break
                    brief["created_at"] = _extract_date_from_filename(fname)
                    items.append(brief)

        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        total = len(items)
        start = (page - 1) * page_size
        end = start + page_size
        page_items = items[start:end]

        briefs = [
            BriefMeta(filename=b["filename"], title=b["title"], created_at=b["created_at"], type=b["type"], snippet=b["snippet"])
            for b in page_items
        ]
        return BriefListResponse(items=briefs, total=total, page=page, page_size=page_size)

    # ── Modo filesystem (local) ──
    items_fs: List[dict] = []

    show_reports = type is None or type in ("report", "all")
    show_synthesis = type in ("synthesis", "all", "orchestrated")

    if show_reports:
        reports_path = Path(REPORTS_DIR)
        if reports_path.exists():
            for f in sorted(reports_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                art = _parse_report_file(f)
                items_fs.append(art)

    if show_synthesis:
        synth_path = Path(SYNTHESIZED_DIR)
        if synth_path.exists():
            for f in sorted(synth_path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                art = _parse_synthesis_file(f)
                if type and art["type"] != type and type != "all":
                    continue
                items_fs.append(art)

    items_fs.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    total = len(items_fs)
    start = (page - 1) * page_size
    end = start + page_size
    page_items = items_fs[start:end]

    briefs = [
        BriefMeta(filename=b["filename"], title=b["title"], created_at=b["created_at"], type=b["type"], snippet=b["snippet"])
        for b in page_items
    ]
    return BriefListResponse(items=briefs, total=total, page=page, page_size=page_size)


def get_brief(filename: str) -> Optional[BriefDetail]:
    """Devuelve un brief completo por nombre de archivo."""

    # ── Modo Supabase ──
    if use_supabase():
        db = get_db()
        storage = get_storage_backend()

        # Buscar en syntheses (por output_filename)
        result = db._client.table("syntheses").select("*").eq("output_filename", filename).execute()
        if result.data:
            brief = _synthesis_row_to_brief(result.data[0])
            return BriefDetail(**brief)

        # Buscar en reports (Storage)
        content = storage.read_report(filename)
        if content:
            brief = {"filename": filename, "title": filename.replace("_", " "), "created_at": "", "type": "report", "snippet": content[:150], "body": content}
            for line in content.split("\n"):
                if line.startswith("# "):
                    brief["title"] = line.lstrip("# ").strip()
                    break
            brief["created_at"] = _extract_date_from_filename(filename)
            return BriefDetail(**brief)

        return None

    # ── Modo filesystem ──
    filepath = Path(SYNTHESIZED_DIR) / filename
    if filepath.exists():
        art = _parse_synthesis_file(filepath)
        return BriefDetail(**art)

    filepath = Path(REPORTS_DIR) / filename
    if filepath.exists():
        art = _parse_report_file(filepath)
        return BriefDetail(**art)

    return None
