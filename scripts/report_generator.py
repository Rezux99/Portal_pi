"""
report_generator.py
Genera informes legibles (Markdown) a partir de los outputs del pipeline.
El JSON es para máquinas. Esto es para humanos.
Sync a Supabase Storage cuando está configurado.
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from scripts.supabase_client import use_supabase
from scripts.supabase_storage import get_storage


from scripts.paths import (
    BASE_DIR, ENTITIES_DIR, CLASSIFIED_DIR, SYNTHESIZED_DIR,
    ACTION_ITEMS_DIR, REPORTS_DIR, RAW_DIR,
)


def _load_latest_json(directory: Path) -> Optional[Dict[str, Any]]:
    """Carga el JSON más reciente de un directorio."""
    if not directory.exists():
        return None
    json_files = sorted(directory.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
    if not json_files:
        return None
    try:
        return json.loads(json_files[0].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_raw_snippets(limit: int = 5) -> List[str]:
    """Carga los primeros párrafos de las noticias raw más recientes."""
    snippets = []
    if not RAW_DIR.exists():
        return snippets
    files = sorted(RAW_DIR.glob("*.txt"), key=lambda x: x.stat().st_mtime, reverse=True)
    for fp in files[:limit]:
        try:
            text = fp.read_text(encoding="utf-8")
            # Extraer título y primeros renglones
            lines = text.strip().split("\n")
            title = ""
            source = ""
            body_lines = []
            for line in lines:
                if line.startswith("TÍTULO:"):
                    title = line.replace("TÍTULO:", "").strip()
                elif line.startswith("FUENTE:"):
                    source = line.replace("FUENTE:", "").strip()
                elif line.startswith("CATEGORÍA:") or line.startswith("ENLACE:") or line.startswith("FECHA"):
                    continue
                else:
                    body_lines.append(line)
            body = " ".join(body_lines).strip()[:300]
            snippet = f"**{title}** — *{source}*\n{body}{'...' if len(body) >= 300 else ''}"
            snippets.append(snippet)
        except OSError:
            continue
    return snippets


def _confidence_emoji(conf: float) -> str:
    if conf is None:
        return "❓"
    if conf >= 0.8:
        return "🟢"
    if conf >= 0.5:
        return "🟡"
    return "🔴"


def _priority_emoji(p: str) -> str:
    p = (p or "").upper()
    if p == "ALTA":
        return "🔴 ALTA"
    if p == "MEDIA":
        return "🟡 MEDIA"
    if p == "BAJA":
        return "🟢 BAJA"
    return p or "—"


def _entity_type_emoji(t: str) -> str:
    t = (t or "").upper()
    emojis = {
        "PERSON": "👤",
        "ORGANIZATION": "🏢",
        "LOCATION": "📍",
        "TECHNOLOGY": "💻",
        "EVENT": "📅",
        "CONCEPT": "💡",
        "NEWS_ITEM": "📰",
    }
    return emojis.get(t, "📌")


def generate_report() -> Path:
    """
    Genera un informe Markdown consolidado con todos los datos del último pipeline run.
    Devuelve la ruta del archivo creado.
    """
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")
    date_human = now.strftime("%d de %B de %Y, %H:%M UTC")

    # ─── Cargar datos ───
    entities_data = _load_latest_json(ENTITIES_DIR)
    classified_data = _load_latest_json(CLASSIFIED_DIR)
    synthesis_data = _load_latest_json(SYNTHESIZED_DIR)
    actions_data = _load_latest_json(ACTION_ITEMS_DIR)

    # ─── Construir informe ───
    lines = []

    # Cabecera
    lines.append("# 🛡 Informe de Inteligencia — Portal Pi")
    lines.append("")
    lines.append(f"> Generado el **{date_human}**")
    lines.append("")

    # ─── 1. Resumen Ejecutivo ───
    lines.append("---")
    lines.append("")
    lines.append("## 📋 Resumen Ejecutivo")
    lines.append("")

    if synthesis_data:
        summary = synthesis_data.get("executive_summary", "")
        if summary:
            lines.append(summary)
        else:
            lines.append("*Sin resumen ejecutivo disponible.*")
        lines.append("")

        priority = synthesis_data.get("priority")
        if priority:
            lines.append(f"**Prioridad:** {_priority_emoji(priority)}")
        trends = synthesis_data.get("trends", [])
        if trends:
            lines.append("")
            lines.append("**Tendencias detectadas:**")
            for t in trends:
                lines.append(f"- {t}")
        lines.append("")
    else:
        lines.append("*No hay síntesis disponible. Ejecuta el pipeline primero.*")
        lines.append("")

    # ─── 2. Clasificación ───
    lines.append("---")
    lines.append("")
    lines.append("## 🏷 Clasificación Temática")
    lines.append("")

    if classified_data:
        cat = classified_data.get("primary_category", "")
        tags = classified_data.get("secondary_tags", [])
        justification = classified_data.get("justification", "")

        if cat:
            lines.append(f"**Categoría principal:** {cat}")
        if tags:
            lines.append(f"**Tags:** {', '.join(tags)}")
        if justification:
            lines.append("")
            lines.append(f"*{justification}*")
        lines.append("")
    else:
        lines.append("*Sin clasificación disponible.*")
        lines.append("")

    # ─── 3. Entidades Detectadas ───
    lines.append("---")
    lines.append("")
    lines.append("## 🔍 Entidades Detectadas")
    lines.append("")

    if entities_data:
        entities = entities_data.get("entities", [])
        relations = entities_data.get("relations", [])

        if entities:
            # Agrupar por tipo
            by_type: Dict[str, list] = {}
            for ent in entities:
                etype = ent.get("type", "OTRO")
                by_type.setdefault(etype, []).append(ent)

            for etype, group in sorted(by_type.items()):
                emoji = _entity_type_emoji(etype)
                lines.append(f"### {emoji} {etype} ({len(group)})")
                lines.append("")
                for ent in group:
                    name = ent.get("name", "?")
                    conf = ent.get("confidence")
                    conf_str = f"{conf:.0%}" if conf is not None else "?"
                    emoji_c = _confidence_emoji(conf) if conf is not None else ""
                    mentions = ent.get("mentions", [])
                    mention_str = ""
                    if mentions:
                        mention_str = f" — mencionado en: {', '.join(str(m) for m in mentions[:3])}"
                    lines.append(f"- {emoji_c} **{name}** ({conf_str} confianza){mention_str}")
                lines.append("")
        else:
            lines.append("*No se detectaron entidades.*")
            lines.append("")

        if relations:
            lines.append("### 🔗 Relaciones entre Entidades")
            lines.append("")
            for rel in relations[:20]:
                subj = rel.get("subject", "?")
                pred = rel.get("predicate", "?")
                obj = rel.get("object", "?")
                lines.append(f"- **{subj}** → *{pred}* → **{obj}**")
            if len(relations) > 20:
                lines.append(f"- ... y {len(relations) - 20} relaciones más")
            lines.append("")
    else:
        lines.append("*Sin entidades disponibles.*")
        lines.append("")

    # ─── 4. Acciones Recomendadas ───
    lines.append("---")
    lines.append("")
    lines.append("## ✅ Acciones Recomendadas")
    lines.append("")

    if actions_data:
        items = actions_data.get("action_items", [])
        if items:
            for i, item in enumerate(items, 1):
                desc = item.get("description", "—")
                priority = item.get("priority", "")
                owner = item.get("owner", "")
                deadline = item.get("deadline", "")
                priority_str = _priority_emoji(priority) if priority else ""
                meta = []
                if owner:
                    meta.append(f"Responsable: {owner}")
                if deadline:
                    meta.append(f"Fecha límite: {deadline}")
                meta_str = f" ({'; '.join(meta)})" if meta else ""
                lines.append(f"{i}. {priority_str} {desc}{meta_str}")
            lines.append("")
        else:
            lines.append("*Sin acciones recomendadas.*")
            lines.append("")
    else:
        lines.append("*Sin acciones disponibles.*")
        lines.append("")

    # ─── 5. Noticias Fuente ───
    lines.append("---")
    lines.append("")
    lines.append("## 📰 Noticias Fuente")
    lines.append("")

    snippets = _load_raw_snippets(limit=5)
    if snippets:
        for s in snippets:
            lines.append(s)
            lines.append("")
    else:
        lines.append("*No hay noticias raw disponibles.*")
        lines.append("")

    # ─── Pie ───
    lines.append("---")
    lines.append("")
    lines.append(f"*Informe generado automáticamente por Portal Pi — {date_human}*")

    # ─── Guardar ───
    report_text = "\n".join(lines)
    report_path = REPORTS_DIR / f"report_{ts}.md"
    report_path.write_text(report_text, encoding="utf-8")

    # Sync to Supabase Storage
    if use_supabase():
        try:
            storage = get_storage()
            storage.save_report(report_path.name, report_text)
        except Exception:
            pass

    return report_path


def list_reports() -> List[Dict[str, Any]]:
    """Lista los informes generados, del más reciente al más antiguo."""
    reports = []
    if not REPORTS_DIR.exists():
        return reports
    for fp in sorted(REPORTS_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            stat = fp.stat()
            # Extraer fecha del filename
            name = fp.stem  # report_20260729_153045
            date_str = ""
            parts = name.split("_")
            if len(parts) >= 3:
                try:
                    date_str = f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:8]} {parts[2][:2]}:{parts[2][2:4]}"
                except (IndexError, ValueError):
                    date_str = ""

            reports.append({
                "filename": fp.name,
                "date": date_str,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
        except OSError:
            continue
    return reports


def read_report(filename: str) -> Optional[str]:
    """Lee el contenido Markdown de un informe."""
    fp = REPORTS_DIR / filename
    if not fp.exists():
        return None
    try:
        return fp.read_text(encoding="utf-8")
    except OSError:
        return None
