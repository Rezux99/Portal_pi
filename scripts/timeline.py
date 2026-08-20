"""
timeline.py
Sistema de memoria a largo plazo basado en Markdown.
Cada pipeline run alimenta una timeline acumulativa y perfiles de entidades.
Esto es lo que convierte un análisis puntual en inteligencia persistente.

Estructura:
  data/timeline/
    ├── index.md              ← Índice maestro
    ├── 2026/
    │   ├── 07-julio.md       ← Timeline mensual
    │   └── 08-agosto.md
    └── entities/
        ├── OpenAI.md         ← Perfil acumulativo de entidad
        └── ciberseguridad.md
"""

import json
import re
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

from scripts.supabase_client import use_supabase
from scripts.supabase_storage import get_storage


from scripts.paths import BASE_DIR, TIMELINE_DIR, ENTITIES_TIMELINE_DIR


def _ensure_dirs() -> None:
    TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
    ENTITIES_TIMELINE_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _now_short() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M")


def _month_path() -> Path:
    """Ruta al archivo de timeline del mes actual."""
    now = datetime.now(timezone.utc)
    year_dir = TIMELINE_DIR / str(now.year)
    year_dir.mkdir(parents=True, exist_ok=True)
    month_names = {
        1: "01-enero", 2: "02-febrero", 3: "03-marzo", 4: "04-abril",
        5: "05-mayo", 6: "06-junio", 7: "07-julio", 8: "08-agosto",
        9: "09-septiembre", 10: "10-octubre", 11: "11-noviembre", 12: "12-diciembre"
    }
    filename = f"{month_names[now.month]}.md"
    return year_dir / filename


def _sanitize_entity_name(name: str) -> str:
    """Convierte un nombre de entidad en un nombre de archivo seguro."""
    name = name.strip()
    # Reemplazar caracteres problemáticos
    name = re.sub(r"[^\w\s\-\.]", "", name)
    name = re.sub(r"\s+", "_", name)
    # Truncar
    if len(name) > 60:
        name = name[:60].rstrip("_")
    return name.lower() or "unknown"


# ═══════════════════════════════════════════════════════════════════════
# ENTRADA EN TIMELINE MENSUAL
# ═══════════════════════════════════════════════════════════════════════

def append_to_timeline(
    summary: str,
    priority: str = "MEDIA",
    entities: Optional[List[str]] = None,
    trends: Optional[List[str]] = None,
    critique_notes: Optional[List[str]] = None,
    source_files: Optional[List[str]] = None,
) -> Path:
    """
    Añade una entrada al timeline del mes actual.
    Devuelve la ruta del archivo modificado.
    """
    _ensure_dirs()
    path = _month_path()

    # Leer existente o crear cabecera
    if path.exists():
        content = path.read_text(encoding="utf-8")
    else:
        now = datetime.now(timezone.utc)
        month_names_es = {
            1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
            5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
            9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
        }
        content = f"# Timeline — {month_names_es[now.month]} {now.year}\n\n"

    # Construir entrada
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    prio_icon = {"ALTA": "🔴", "MEDIA": "🟡", "BAJA": "🟢"}.get(priority, "⚪")

    entry = f"\n## {date_str} — {time_str} UTC\n\n"
    entry += f"{prio_icon} **Prioridad: {priority}**\n\n"
    entry += f"{summary}\n\n"

    if entities:
        entry += "**Entidades:** " + ", ".join(f"`{e}`" for e in entities[:10]) + "\n\n"
    if trends:
        entry += "**Tendencias:** " + ", ".join(f"*{t}*" for t in trends[:5]) + "\n\n"
    if critique_notes:
        entry += "**⚠ Notas de la crítica:**\n"
        for note in critique_notes[:5]:
            entry += f"- {note}\n"
        entry += "\n"
    if source_files:
        entry += "**Fuentes:** " + ", ".join(f"`{f}`" for f in source_files[:5]) + "\n\n"

    entry += "---\n"

    path.write_text(content + entry, encoding="utf-8")

    # Sync to Supabase Storage
    if use_supabase():
        try:
            storage = get_storage()
            rel = str(path.relative_to(TIMELINE_DIR)).replace("\\", "/")
            storage.save_timeline(rel, content + entry)
        except Exception:
            pass

    return path


# ═══════════════════════════════════════════════════════════════════════
# PERFIL DE ENTIDAD (acumulativo)
# ═══════════════════════════════════════════════════════════════════════

def update_entity_profile(
    name: str,
    entity_type: str = "",
    confidence: float = 0.0,
    context: str = "",
    relations: Optional[List[Dict[str, str]]] = None,
) -> Path:
    """
    Actualiza (o crea) el perfil Markdown de una entidad.
    Los perfiles son acumulativos: cada mención añade contexto, no sobreescribe.
    """
    _ensure_dirs()
    safe_name = _sanitize_entity_name(name)
    profile_path = ENTITIES_TIMELINE_DIR / f"{safe_name}.md"

    now_str = _now()

    if profile_path.exists():
        content = profile_path.read_text(encoding="utf-8")
    else:
        type_emoji = {
            "PERSON": "👤", "ORGANIZATION": "🏢", "LOCATION": "📍",
            "TECHNOLOGY": "💻", "EVENT": "📅", "CONCEPT": "💡"
        }.get(entity_type.upper(), "📌")
        content = f"# {type_emoji} {name}\n\n"
        content += f"- **Tipo:** {entity_type or 'Desconocido'}\n"
        content += f"- **Primera mención:** {now_str}\n"
        content += f"- **Menciones:** 0\n\n"
        content += "---\n\n"

    # Incrementar contador de menciones
    mentions_match = re.search(r"\*\*Menciones:\*\*\s*(\d+)", content)
    if mentions_match:
        count = int(mentions_match.group(1)) + 1
        content = content.replace(f"**Menciones:** {mentions_match.group(1)}", f"**Menciones:** {count}")

    # Añadir mención
    mention_entry = f"\n## Mención — {now_str}\n\n"
    if confidence:
        conf_pct = f"{confidence:.0%}" if isinstance(confidence, float) else str(confidence)
        mention_entry += f"- **Confianza:** {conf_pct}\n"
    if context:
        mention_entry += f"- **Contexto:** {context}\n"
    if relations:
        mention_entry += "- **Relaciones en esta mención:**\n"
        for rel in relations[:5]:
            mention_entry += f"  - {rel.get('subject', '?')} → *{rel.get('predicate', '?')}* → {rel.get('object', '?')}\n"
    mention_entry += "\n---\n"

    profile_path.write_text(content + mention_entry, encoding="utf-8")

    # Sync to Supabase Storage
    if use_supabase():
        try:
            storage = get_storage()
            storage.save_entity_profile(profile_path.name, content + mention_entry)
        except Exception:
            pass

    return profile_path


# ═══════════════════════════════════════════════════════════════════════
# ALIMENTAR DESDE RESULTADOS DEL PIPELINE
# ═══════════════════════════════════════════════════════════════════════

def feed_from_pipeline(
    entities_data: Optional[Dict[str, Any]] = None,
    classified_data: Optional[Dict[str, Any]] = None,
    synthesis_data: Optional[Dict[str, Any]] = None,
    critique_data: Optional[Dict[str, Any]] = None,
    source_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Alimenta la timeline y los perfiles de entidades con los resultados de un pipeline run.
    Devuelve un resumen de lo que se actualizó.
    """
    _ensure_dirs()
    result = {"timeline_path": None, "entity_profiles": [], "errors": []}

    # ── Timeline mensual ──
    try:
        summary = ""
        priority = "MEDIA"
        entity_names = []
        trends = []
        critique_notes = []

        if synthesis_data:
            summary = synthesis_data.get("executive_summary", "")
            priority = synthesis_data.get("priority", "MEDIA")
            trends = synthesis_data.get("trends", [])
            # Normalizar trends si es JSON string
            if isinstance(trends, str):
                try:
                    trends = json.loads(trends)
                except:
                    trends = [trends]

        if entities_data:
            entities_list = entities_data.get("entities", [])
            entity_names = [e.get("name", "") for e in entities_list if e.get("name")]

        if critique_data:
            critique_notes = critique_data.get("blind_spots", []) + critique_data.get("questions_to_investigate", [])
            # Limitar
            critique_notes = critique_notes[:5]

        if not summary:
            summary = "Pipeline ejecutado sin resumen ejecutivo disponible."

        timeline_path = append_to_timeline(
            summary=summary,
            priority=priority,
            entities=entity_names,
            trends=trends,
            critique_notes=critique_notes,
            source_files=source_files,
        )
        result["timeline_path"] = str(timeline_path)
    except Exception as e:
        result["errors"].append(f"Timeline: {e}")

    # ── Perfiles de entidades ──
    if entities_data:
        entities_list = entities_data.get("entities", [])
        relations_list = entities_data.get("relations", [])
        for ent in entities_list:
            try:
                name = ent.get("name", "")
                if not name:
                    continue
                # Buscar relaciones que involucren esta entidad
                entity_relations = [
                    r for r in relations_list
                    if r.get("subject") == name or r.get("object") == name
                ][:3]
                profile_path = update_entity_profile(
                    name=name,
                    entity_type=ent.get("type", ""),
                    confidence=ent.get("confidence", 0.0),
                    context=f"Mencionado en pipeline run del {_now()}",
                    relations=entity_relations,
                )
                result["entity_profiles"].append(str(profile_path))
            except Exception as e:
                result["errors"].append(f"Entity {ent.get('name', '?')}: {e}")

    return result


# ═══════════════════════════════════════════════════════════════════════
# ÍNDICE MAESTRO
# ═══════════════════════════════════════════════════════════════════════

def rebuild_index() -> Path:
    """Reconstruye el índice maestro de la timeline."""
    _ensure_dirs()
    index_path = TIMELINE_DIR / "index.md"

    lines = ["# 📚 Timeline — Índice Maestro\n"]
    lines.append(f"*Última actualización: {_now()}*\n\n")

    # Buscar archivos mensuales
    months = []
    for year_dir in sorted(TIMELINE_DIR.glob("*")):
        if not year_dir.is_dir() or year_dir.name == "entities":
            continue
        for month_file in sorted(year_dir.glob("*.md"), reverse=True):
            months.append(month_file)

    if months:
        lines.append("## 📅 Timeline Mensual\n\n")
        for m in months:
            # Extraer año y mes del path
            parts = m.parts
            year = parts[-2] if len(parts) >= 2 else "?"
            month_name = m.stem.replace("-", " ", 1).title()
            lines.append(f"- [{month_name} {year}]({m.relative_to(TIMELINE_DIR).as_posix()})\n")
        lines.append("\n")

    # Entidades con perfil
    entity_files = sorted(ENTITIES_TIMELINE_DIR.glob("*.md"))
    if entity_files:
        lines.append(f"## 🔍 Entidades Rastreadas ({len(entity_files)})\n\n")
        for ef in entity_files[:50]:
            name = ef.stem.replace("_", " ").title()
            lines.append(f"- [{name}](entities/{ef.name})\n")
        if len(entity_files) > 50:
            lines.append(f"\n... y {len(entity_files) - 50} más\n")
        lines.append("\n")

    index_path.write_text("".join(lines), encoding="utf-8")
    return index_path


def get_timeline_summary() -> Dict[str, Any]:
    """Devuelve un resumen del estado de la timeline."""
    _ensure_dirs()
    months = []
    for year_dir in sorted(TIMELINE_DIR.glob("*")):
        if not year_dir.is_dir() or year_dir.name == "entities":
            continue
        for month_file in sorted(year_dir.glob("*.md"), reverse=True):
            try:
                size = month_file.stat().st_size
                content = month_file.read_text(encoding="utf-8")
                entries = content.count("## 20")  # contar entradas por fecha
                months.append({
                    "path": str(month_file.relative_to(TIMELINE_DIR)),
                    "filename": month_file.name,
                    "year": month_file.parent.name,
                    "entries": entries,
                    "size_bytes": size,
                })
            except:
                pass

    entity_count = len(list(ENTITIES_TIMELINE_DIR.glob("*.md")))

    return {
        "months": months,
        "total_months": len(months),
        "total_entities_tracked": entity_count,
        "index_exists": (TIMELINE_DIR / "index.md").exists(),
    }
