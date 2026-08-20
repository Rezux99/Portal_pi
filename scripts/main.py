"""
main.py
Orquestador principal de Portal Pi.
Bucle CLI stateless: lee disco, ensambla contexto, invoca LLM, rutea output.
"""

import os
import sys
import json
import shutil
import argparse
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable
from datetime import datetime

from scripts.state_manager import StateManager, StateManagerError
from scripts.database import PortalDatabase, DatabaseError
from scripts.supabase_client import use_supabase
from scripts.supabase_database import SupabaseDatabase
from scripts.supabase_storage import get_storage


# ─── RUTAS (source of truth: scripts/paths.py) ─────────────────────────────
from scripts.paths import (
    BASE_DIR, STATE_PATH, DB_PATH, RAW_DIR, PROCESSED_DIR,
    SYNTHESIZED_DIR, ENTITIES_DIR, CLASSIFIED_DIR, ACTION_ITEMS_DIR,
    LOGS_DIR, REPORTS_DIR, TIMELINE_DIR, ENTITIES_TIMELINE_DIR,
    ORCHESTRATOR_LOG, log_to_file,
)


def _use_supabase() -> bool:
    return use_supabase()

def _get_db():
    if _use_supabase():
        return SupabaseDatabase()
    return PortalDatabase(str(DB_PATH))

def _get_storage():
    return get_storage()


db = _get_db()


def log_event(message: str, level: str = "INFO") -> None:
    """Log al archivo del orquestador. Wrapper de log_to_file."""
    log_to_file(ORCHESTRATOR_LOG, message, level)


# ─── ENSAMBLADOR DE CONTEXTO ──────────────────────────────────────────────────

def read_raw_news_files(limit: int = 3, skip_processed: bool = True) -> List[Dict[str, Any]]:
    """Lee archivos de raw_news que estén en la BD y no procesados.

    Usa la BD para tracking (processed_at). Ordena por ingested_at DESC.
    Solo devuelve archivos que estén en la tabla raw_news (para poder marcarlos).
    """

    # ── Modo Supabase ──
    if _use_supabase():
        try:
            db_local = _get_db()
            unprocessed = db_local.get_unprocessed_filenames()[:limit]
            result: List[Dict[str, Any]] = []
            for name in unprocessed:
                content = db_local._client.table("raw_news").select("content").eq("filename", name).execute()
                if content.data:
                    result.append({"filename": name, "content": content.data[0]["content"]})
            return result
        except Exception as exc:
            log_event(f"Error leyendo raw_news desde Supabase: {exc}", "WARN")

    # ── Modo filesystem ──
    if not RAW_DIR.exists():
        return []

    # ── Obtener filenames no procesados desde la BD ──
    unprocessed = None
    if skip_processed:
        try:
            with db._get_conn() as conn:
                rows = conn.execute(
                    "SELECT filename FROM raw_news WHERE processed_at IS NULL ORDER BY ingested_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
                unprocessed = [r[0] if isinstance(r[0], str) else r['filename'] for r in rows]
        except Exception:
            unprocessed = None

    # ── Si no hay BD o skip_processed=False, leer de disco ──
    if unprocessed is None:
        files: List[Dict[str, Any]] = []
        for fp in sorted(RAW_DIR.iterdir(), reverse=True):
            if fp.suffix.lower() in (".txt", ".md"):
                try:
                    content = fp.read_text(encoding="utf-8")
                    files.append({"filename": fp.name, "content": content})
                    if len(files) >= limit:
                        break
                except (OSError, IOError) as exc:
                    log_event(f"No se pudo leer {fp.name}: {exc}", "WARN")
        return files

    # ── Leer contenido de los archivos no procesados ──
    result: List[Dict[str, Any]] = []
    for name in unprocessed:
        fp = RAW_DIR / name
        if fp.exists():
            try:
                content = fp.read_text(encoding="utf-8")
                result.append({"filename": name, "content": content})
            except (OSError, IOError) as exc:
                log_event(f"No se pudo leer {name}: {exc}", "WARN")
    return result


def read_processed_context() -> str:
    """Lee resúmenes ya procesados para inyectar como contexto histórico."""
    chunks = []
    if PROCESSED_DIR.exists():
        for fp in sorted(PROCESSED_DIR.iterdir()):
            if fp.suffix.lower() == ".json":
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    chunks.append(f"--- {fp.name} ---\n{json.dumps(data, ensure_ascii=False)}")
                except (OSError, IOError, json.JSONDecodeError) as exc:
                    log_event(f"Contexto corrupto {fp.name}: {exc}", "WARN")
    return "\n\n".join(chunks) if chunks else "[SIN_CONTEXTO_HISTORICO]"


def assemble_dynamic_prompt(state: Dict[str, Any], command: str, payload: Optional[str] = None) -> str:
    """
    Ensambla el System Prompt masivo inyectando variables desde disco.
    """
    pointers = state.get("execution_pointers", {})
    stage = pointers.get("current_pipeline_stage", "IDLE")
    raw_files = read_raw_news_files(limit=3)
    historical = read_processed_context()

    raw_injection = "\n\n".join(
        f"--- ARCHIVO: {r['filename']} ---\n{r['content'][:2000]}"
        for r in raw_files
    ) if raw_files else "[SIN_NOTICIAS_RAW]"

    # ─── Schemas estrictos por comando ───
    command_schemas = {
        "/extract_entities": """{ "status": "ok", "data": { "source_file": "<filename>", "output_filename": "entities.json", "entities": [ { "name": "<nombre_entidad>", "type": "PERSON|ORGANIZATION|LOCATION|TECHNOLOGY|EVENT|CONCEPT|...", "confidence": 0.0-1.0, "mentions": ["mención1","mención2"] } ], "relations": [ { "subject": "<entidad1>", "predicate": "<relación>", "object": "<entidad2>" } ] }, "audit_note": "<resumen de la extracción>" }""",

        "/classify_topic": """{ "status": "ok", "data": { "source_file": "<filename>", "output_filename": "classified.json", "primary_category": "<categoría principal>", "secondary_tags": ["<tag1>","<tag2>"], "justification": "<por qué esta clasificación>" }, "audit_note": "<resumen>" }""",

        "/synthesize_news": """{ "status": "ok", "data": { "source_files": ["<file1>","<file2>"], "output_filename": "synthesis.json", "executive_summary": "<resumen ejecutivo de 3-5 frases>", "priority": "ALTA|MEDIA|BAJA", "trends": ["<tendencia1>","<tendencia2>"] }, "audit_note": "<resumen>" }""",

        "/generate_action_items": """{ "status": "ok", "data": { "source_file": "<filename>", "output_filename": "actions.json", "action_items": [ { "id": "ACT-001", "description": "<descripción accionable>", "owner": "<responsable>", "deadline": "<fecha>", "priority": "ALTA|MEDIA|BAJA" } ] }, "audit_note": "<resumen>" }""",

        "/audit_state": """{ "status": "ok", "data": { "summary": "<resumen del estado>", "integrity": "OK|WARN|ERROR" }, "audit_note": "<resumen>" }""",
    }

    schema_instruction = command_schemas.get(command, "")

    prompt = f"""[PORTAL_PI_SYSTEM_PROMPT]
Eres el motor de extracción y análisis de Portal Pi. Operas en modo STATELESS.
NO inventes datos. Si no hay información, responde con campos vacíos o null.

[ESTADO_ACTUAL]
- Etapa pipeline: {stage}
- Puntero archivo raw: {pointers.get('current_raw_file', 'Ninguno')}
- Última tarea completada: {pointers.get('last_completed_task', 'Ninguna')}

[CONTEXTO_HISTORICO_PROCESADO]
{historical}

[NOTICIAS_RAW_DISPONIBLES]
{raw_injection}

[COMANDO_MICRO_TAREA]
{command}

[PAYLOAD_ADICIONAL]
{payload if payload else '[NINGUNO]'}

[SCHEMA_DE_SALIDA_OBLIGATORIO]
Tu respuesta DEBE seguir EXACTAMENTE este schema JSON. No añadas claves extra, no uses "noticias" ni ninguna otra estructura.
{schema_instruction}

[RESTRICCIONES_DE_SALIDA]
- Responde ÚNICAMENTE en JSON válido, sin texto antes ni después.
- No incluyas markdown de bloque de código (no ```json).
- El JSON debe ser parseable por json.loads() de Python sin preprocesamiento.
- La clave raíz "status" debe ser "ok" o "error".
- La clave raíz "data" debe contener los campos del schema de arriba.
- La clave raíz "audit_note" debe ser un string con un resumen breve.
"""
    return prompt


# ─── LLM INTERFACE (INTERACTIVA) ─────────────────────────────────────────────

def call_llm(prompt: str) -> Dict[str, Any]:
    """
    Modo interactivo: muestra el prompt al operador para que lo lleve
    a la IA de su elección, y luego pegue la respuesta JSON aquí.
    """
    SEPARATOR = "=" * 60
    print(f"\n{SEPARATOR}")
    print("PROMPT PARA LLM — Copia el texto de abajo y llévalo a tu IA")
    print(f"{SEPARATOR}")
    print(prompt)
    print(f"{SEPARATOR}")
    print("FIN DEL PROMPT")
    print(f"{SEPARATOR}\n")

    print("Pega la respuesta JSON del LLM (o 'q' para cancelar):")
    lines = []
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print("Cancelado.")
            return {"status": "error", "data": {}, "audit_note": "Operador canceló la entrada."}

        stripped = line.strip()
        if stripped.lower() == "q":
            return {"status": "error", "data": {}, "audit_note": "Operador canceló la entrada."}
        lines.append(line)

        # Detectar cierre de JSON raíz
        raw = "\n".join(lines)
        try:
            parsed = json.loads(raw)
            return parsed
        except json.JSONDecodeError:
            continue

    # No debería llegar aquí, pero por seguridad
    return {"status": "error", "data": {}, "audit_note": "Entrada inválida."}


# ─── RUTEADOR DE RESPUESTA ──────────────────────────────────────────────────

def _save_json_file(directory: Path, filename: str, data: Dict[str, Any]) -> Path:
    """Guarda JSON en disco y devuelve la ruta. Si Supabase está activo, sincroniza a Storage."""
    out_path = directory / filename
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Sync to Supabase Storage
    if _use_supabase():
        try:
            storage = _get_storage()
            rel_path = str(out_path.relative_to(BASE_DIR)).replace("\\", "/")
            content_str = json.dumps(data, indent=2, ensure_ascii=False)
            storage.save_pipeline_output(rel_path, content_str)
        except Exception as exc:
            log_event(f"Error sync a Supabase Storage ({filename}): {exc}", "WARN")

    return out_path


def _versioned_filename(base_name: str) -> str:
    """Genera un nombre de archivo versionado con timestamp.
    Ej: entities.json → entities_20260729_153045.json
    Si base_name ya tiene timestamp, se deja tal cual.
    """
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix or ".json"
    # Si ya tiene un timestamp en el nombre (8 dígitos + _ + 6 dígitos), no añadir otro
    import re
    if re.search(r"_\d{8}_\d{6}$", stem):
        return base_name
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"{stem}_{ts}{suffix}"


def route_response(state_mgr: StateManager, command: str, llm_response: Dict[str, Any]) -> None:
    """
    Interpreta la respuesta JSON del LLM y actualiza estado / mueve archivos en disco / alimenta la DB.
    Soporta respuestas con schema normalizado ({status, data, ...}) y respuestas sin wrapper.
    Cada ejecución guarda archivos versionados con timestamp para no sobreescribir runs anteriores.
    """
    if llm_response.get("status") != "ok":
        state_mgr.set_flag("global_status", "ERROR")
        state_mgr.append_audit({
            "event": "llm_error",
            "command": command,
            "detail": llm_response.get("audit_note", "Error desconocido")
        })
        log_event("LLM devolvió estado de error.", "ERROR")
        return

    data = llm_response.get("data", {})
    source_file = data.get("source_file", "unknown")

    # ── Intentar inferir source_file de los archivos raw ──
    if source_file == "unknown":
        raw_files = read_raw_news_files(limit=1)
        if raw_files:
            source_file = raw_files[0]["filename"]

    if command == "/extract_entities":
        # ── Normalizar claves del LLM ──
        entities = data.get("entities", [])
        relations = data.get("relations", [])

        # Fallback: si el LLM usó "noticias" en vez de "entities"
        if not entities and "noticias" in data:
            raw_items = data["noticias"]
            entities = []
            for item in raw_items:
                # Crear entidad a partir de cada noticia
                name = item.get("titulo", item.get("title", ""))
                if name:
                    entities.append({
                        "name": name[:200],
                        "type": "NEWS_ITEM",
                        "confidence": 0.7,
                        "mentions": [item.get("fuente", item.get("source", ""))]
                    })
                # Extraer fuente como entidad
                fuente = item.get("fuente", item.get("source", ""))
                if fuente:
                    entities.append({
                        "name": fuente,
                        "type": "ORGANIZATION",
                        "confidence": 0.9,
                        "mentions": []
                    })
            # Extraer tema_principal y subtemas como entidades
            if data.get("tema_principal"):
                entities.append({
                    "name": data["tema_principal"],
                    "type": "CONCEPT",
                    "confidence": 0.8,
                    "mentions": []
                })
            for st in data.get("subtemas", []):
                entities.append({
                    "name": st,
                    "type": "CONCEPT",
                    "confidence": 0.6,
                    "mentions": []
                })

        filename = _versioned_filename(data.get("output_filename", "entities.json"))
        try:
            # Guardar en disco con estructura correcta
            save_data = {"entities": entities, "relations": relations, "source_file": source_file}
            out_path = _save_json_file(ENTITIES_DIR, filename, save_data)
            state_mgr.register_file("processed", filename, {"type": "entities", "source": "llm"})
            state_mgr.set_execution_pointer("last_completed_task", "/extract_entities")
            # ── Alimentar DB ──
            n_ent = db.insert_entities(entities, source_file)
            n_rel = db.insert_relations(relations, source_file)
            log_event(f"Entidades guardadas en {out_path} (DB: {n_ent} entidades, {n_rel} relaciones)")
        except (OSError, IOError, DatabaseError) as exc:
            log_event(f"Fallo al escribir entidades: {exc}", "ERROR")

    elif command == "/synthesize_news":
        # ── Normalizar claves del LLM ──
        executive_summary = data.get("executive_summary", "")
        priority = data.get("priority")
        trends = data.get("trends", [])
        source_files = data.get("source_files", [])

        # Fallback: si el LLM usó "noticias" sin executive_summary
        if not executive_summary and "noticias" in data:
            noticias = data["noticias"]
            titles = [n.get("titulo", n.get("title", "")) for n in noticias[:5]]
            executive_summary = f"Síntesis de {len(noticias)} noticias: " + "; ".join(titles)
            if not priority:
                priority = "MEDIA"
            if not trends:
                trends = [data.get("tema_principal", "General")]
            if not source_files:
                source_files = [source_file]

        filename = _versioned_filename(data.get("output_filename", "synthesis.json"))
        try:
            save_data = {
                "executive_summary": executive_summary,
                "priority": priority,
                "trends": trends,
                "source_files": source_files,
                "output_filename": filename,
            }
            out_path = _save_json_file(SYNTHESIZED_DIR, filename, save_data)
            state_mgr.register_file("synthesized", filename, {"type": "synthesis", "source": "llm"})
            state_mgr.set_execution_pointer("last_completed_task", "/synthesize_news")
            # ── Alimentar DB ──
            row_id = db.insert_synthesis(save_data)
            log_event(f"Síntesis guardada en {out_path} (DB: fila {row_id})")
        except (OSError, IOError, DatabaseError) as exc:
            log_event(f"Fallo al escribir síntesis: {exc}", "ERROR")

    elif command == "/classify_topic":
        # ── Normalizar claves del LLM ──
        primary_category = data.get("primary_category", "")
        secondary_tags = data.get("secondary_tags", [])
        justification = data.get("justification", "")

        # Fallback: si el LLM usó claves en español
        if not primary_category:
            primary_category = data.get("categoria", data.get("tema_principal", data.get("category", "")))
        if not secondary_tags:
            secondary_tags = data.get("subtemas", data.get("tags", []))
        if not justification:
            justification = data.get("justificacion", "")

        # Último fallback: inferir de las noticias
        if not primary_category and "noticias" in data:
            cats = set()
            for n in data["noticias"]:
                cat = n.get("categoria", n.get("category", ""))
                if cat:
                    cats.add(cat)
            primary_category = ", ".join(cats) if cats else "Sin categoría"

        filename = _versioned_filename(data.get("output_filename", "classified.json"))
        try:
            save_data = {
                "primary_category": primary_category,
                "secondary_tags": secondary_tags,
                "justification": justification,
                "source_file": source_file,
            }
            out_path = _save_json_file(CLASSIFIED_DIR, filename, save_data)
            state_mgr.register_file("processed", filename, {"type": "classified", "source": "llm"})
            state_mgr.set_execution_pointer("last_completed_task", "/classify_topic")
            # ── Alimentar DB ──
            row_id = db.insert_classification(save_data, source_file)
            log_event(f"Clasificación guardada en {out_path} (DB: fila {row_id})")
        except (OSError, IOError, DatabaseError) as exc:
            log_event(f"Fallo al escribir clasificación: {exc}", "ERROR")

    elif command == "/generate_action_items":
        # ── Normalizar claves del LLM ──
        items = data.get("action_items", [])

        # Fallback: si el LLM usó "noticias" o "acciones"
        if not items and "acciones" in data:
            items = data["acciones"]
        if not items and "noticias" in data:
            items = []
            for i, n in enumerate(data["noticias"][:5]):
                items.append({
                    "id": f"ACT-{i+1:03d}",
                    "description": n.get("titulo", n.get("title", "Revisar noticia")),
                    "owner": "",
                    "deadline": "",
                    "priority": "MEDIA"
                })

        filename = _versioned_filename(data.get("output_filename", "actions.json"))
        try:
            save_data = {"action_items": items, "source_file": source_file}
            out_path = _save_json_file(ACTION_ITEMS_DIR, filename, save_data)
            state_mgr.register_file("processed", filename, {"type": "action_items", "source": "llm"})
            state_mgr.set_execution_pointer("last_completed_task", "/generate_action_items")
            # ── Alimentar DB ──
            n = db.insert_action_items(items, filename)
            log_event(f"Action items guardados en {out_path} (DB: {n} items)")
        except (OSError, IOError, DatabaseError) as exc:
            log_event(f"Fallo al escribir action items: {exc}", "ERROR")

    elif command == "/audit_state":
        state_mgr.append_audit({
            "event": "manual_audit",
            "llm_observation": data.get("summary", "Sin observaciones"),
            "integrity_check": data.get("integrity", "unknown")
        })
        state_mgr.set_execution_pointer("last_completed_task", "/audit_state")
        log_event("Auditoría registrada.")

    else:
        log_event(f"Comando {command} no requiere ruteo de archivos.", "WARN")

    state_mgr.append_audit({
        "event": "command_routed",
        "command": command,
        "status": "completed"
    })


# ─── BUCLE PRINCIPAL CMD ─────────────────────────────────────────────────────

class PortalPiCLI:
    def __init__(self) -> None:
        self.state_mgr = StateManager(str(STATE_PATH))
        self.running = True
        self.commands: Dict[str, Callable[[Optional[str]], None]] = {
            "/extract_entities": self.cmd_extract,
            "/synthesize_news": self.cmd_synthesize,
            "/classify_topic": self.cmd_classify,
            "/generate_action_items": self.cmd_action_items,
            "/audit_state": self.cmd_audit,
            "/show_state": self.cmd_show_state,
            "/run_pipeline": self.cmd_run_pipeline,
            "/db": self.cmd_db,
            "/query": self.cmd_query,
            "/advance": self.cmd_advance,
            "/help": self.cmd_help,
            "/exit": self.cmd_exit,
        }

    def _run_pipeline(self, command: str, payload: Optional[str] = None) -> None:
        try:
            state = self.state_mgr.get_full_state()
            prompt = assemble_dynamic_prompt(state, command, payload)
            llm_response = call_llm(prompt)
            route_response(self.state_mgr, command, llm_response)
        except StateManagerError as exc:
            log_event(f"Error de estado: {exc}", "ERROR")
        except json.JSONDecodeError as exc:
            log_event(f"Respuesta LLM no es JSON válido: {exc}", "ERROR")
        except Exception as exc:
            log_event(f"Excepción no controlada: {exc}", "CRITICAL")

    def cmd_extract(self, payload: Optional[str]) -> None:
        self.state_mgr.transition_stage("EXTRACTION")
        raw_files = read_raw_news_files(limit=1)
        if raw_files:
            self.state_mgr.set_execution_pointer("current_raw_file", raw_files[0]["filename"])
        self._run_pipeline("/extract_entities", payload)

    def cmd_synthesize(self, payload: Optional[str]) -> None:
        self.state_mgr.transition_stage("SYNTHESIS")
        self._run_pipeline("/synthesize_news", payload)

    def cmd_classify(self, payload: Optional[str]) -> None:
        self._run_pipeline("/classify_topic", payload)

    def cmd_action_items(self, payload: Optional[str]) -> None:
        self._run_pipeline("/generate_action_items", payload)

    def cmd_audit(self, payload: Optional[str]) -> None:
        self.state_mgr.transition_stage("AUDIT")
        self._run_pipeline("/audit_state", payload)

    def cmd_show_state(self, payload: Optional[str]) -> None:
        state = self.state_mgr.get_full_state()
        print(json.dumps(state, indent=2, ensure_ascii=False))

    def cmd_run_pipeline(self, payload: Optional[str]) -> None:
        """
        Ejecuta el pipeline completo paso a paso.
        En cada paso se muestra el prompt, el operador pega la respuesta real del LLM.
        Si no hay datos de entrada para un paso, se salta automáticamente.
        Con 'q' se salta un paso manualmente sin contaminar la DB.
        """
        SEPARATOR = "=" * 60
        stats_before = db.stats()
        processed = []

        # ── Verificar que hay raw_news ──
        raw_files = read_raw_news_files(limit=10)
        if not raw_files:
            print("\n  No hay noticias raw en data/raw_news/. Coloca archivos .txt o .md ahí antes de ejecutar el pipeline.")
            return

        print(f"\n{SEPARATOR}")
        print("  PIPELINE AUTOMATICO — Portal Pi")
        print(f"{SEPARATOR}")
        print(f"  Noticias raw detectadas: {len(raw_files)}")
        for rf in raw_files:
            print(f"    - {rf['filename']}")
        print(f"{SEPARATOR}")

        steps = [
            (1, "/extract_entities", "Extrayendo entidades y relaciones", "EXTRACTION", True),
            (2, "/classify_topic", "Clasificando por tema", None, False),
            (3, "/synthesize_news", "Sintetizando noticias", "SYNTHESIS", True),
            (4, "/generate_action_items", "Generando action items", None, False),
        ]

        total_steps = len(steps)

        for step_num, command, description, stage, needs_raw in steps:
            print(f"\n{SEPARATOR}")
            print(f"  PASO {step_num}/{total_steps}: {description}")
            print(f"{SEPARATOR}")

            # ── Verificar datos de entrada ──
            if needs_raw:
                current_raw = read_raw_news_files(limit=3)
                if not current_raw:
                    print("  SIN DATOS — No hay noticias raw disponibles. Saltando paso.")
                    processed.append({"step": command, "status": "skipped", "reason": "sin datos raw"})
                    continue

            # ── Transición de etapa si aplica ──
            if stage:
                try:
                    self.state_mgr.transition_stage(stage)
                except StateManagerError:
                    pass  # Ya estamos en la etapa correcta, continuar

            # ── Ejecutar paso ──
            try:
                state = self.state_mgr.get_full_state()
                prompt = assemble_dynamic_prompt(state, command)
                llm_response = call_llm(prompt)

                if llm_response.get("status") == "error" and llm_response.get("audit_note", "") == "Operador canceló la entrada.":
                    print("  Paso saltado por el operador.")
                    processed.append({"step": command, "status": "skipped", "reason": "operador canceló"})
                    continue

                route_response(self.state_mgr, command, llm_response)
                processed.append({"step": command, "status": "ok"})

            except (StateManagerError, json.JSONDecodeError, DatabaseError) as exc:
                log_event(f"Error en paso {command}: {exc}", "ERROR")
                processed.append({"step": command, "status": "error", "reason": str(exc)})
            except Exception as exc:
                log_event(f"Excepción en paso {command}: {exc}", "CRITICAL")
                processed.append({"step": command, "status": "error", "reason": str(exc)})

        # ── Resumen final ──
        stats_after = db.stats()
        print(f"\n{SEPARATOR}")
        print("  RESUMEN DEL PIPELINE")
        print(f"{SEPARATOR}")
        for p in processed:
            icon = {"ok": "OK", "skipped": "SKIP", "error": "ERR"}[p["status"]]
            reason = f" ({p['reason']})" if "reason" in p else ""
            print(f"  [{icon}] {p['step']}{reason}")
        print(f"{SEPARATOR}")
        print("  Cambios en DB:")
        for table in stats_after:
            diff = stats_after[table] - stats_before.get(table, 0)
            if diff > 0:
                print(f"    +{diff} filas en {table}")
        print(f"{SEPARATOR}")
        print("  Trazabilidad — Archivos raw utilizados:")
        for rf in raw_files:
            print(f"    - {rf['filename']}")
        print(f"{SEPARATOR}")

        # ── Volver a IDLE ──
        try:
            self.state_mgr.transition_stage("IDLE")
        except StateManagerError:
            pass

    def cmd_advance(self, payload: Optional[str]) -> None:
        target = payload.strip() if payload else None
        if not target:
            print("Uso: /advance <STAGE>")
            return
        try:
            self.state_mgr.transition_stage(target)
            log_event(f"Etapa avanzada manualmente a {target}")
        except StateManagerError as exc:
            log_event(str(exc), "ERROR")

    def _print_table(self, rows: List[Dict[str, Any]], max_col_width: int = 60) -> None:
        """Imprime una lista de diccionarios como tabla legible."""
        if not rows:
            print("  (sin resultados)")
            return
        columns = list(rows[0].keys())
        # Header
        header = " | ".join(columns)
        sep = "-+-".join("-" * min(len(c), max_col_width) for c in columns)
        print(f"  {header}")
        print(f"  {sep}")
        # Rows
        for row in rows:
            values = []
            for c in columns:
                v = str(row.get(c, ""))
                if len(v) > max_col_width:
                    v = v[:max_col_width - 3] + "..."
                values.append(v)
            print("  " + " | ".join(values))
        print(f"  ({len(rows)} filas)")

    def cmd_db(self, payload: Optional[str]) -> None:
        """Consulta la base de datos: /db <entities|relations|syntheses|classifications|actions|stats>"""
        subcmd = payload.strip().lower() if payload else None
        if not subcmd:
            print("Uso: /db <entities|relations|syntheses|classifications|actions|stats>")
            return
        try:
            if subcmd == "entities":
                self._print_table(db.list_entities())
            elif subcmd == "relations":
                self._print_table(db.list_relations())
            elif subcmd == "syntheses":
                self._print_table(db.list_syntheses())
            elif subcmd == "classifications":
                self._print_table(db.list_classifications())
            elif subcmd == "actions":
                self._print_table(db.list_action_items())
            elif subcmd == "stats":
                stats = db.stats()
                for table, count in stats.items():
                    print(f"  {table}: {count} filas")
            elif subcmd == "search":
                # /db search entities <nombre>
                print("Uso: /db search <entities|relations> <término>")
            else:
                print(f"Subcomando desconocido: {subcmd}")
                print("Disponibles: entities, relations, syntheses, classifications, actions, stats, search")
        except DatabaseError as exc:
            log_event(f"Error de DB: {exc}", "ERROR")

    def cmd_query(self, payload: Optional[str]) -> None:
        """Ejecuta una consulta SQL directa: /query <SQL>"""
        sql = payload.strip() if payload else None
        if not sql:
            print("Uso: /query <sentencia SQL SELECT>")
            return
        if not sql.upper().startswith("SELECT"):
            print("Solo se permiten consultas SELECT.")
            return
        try:
            rows = db.query(sql)
            self._print_table(rows)
        except DatabaseError as exc:
            log_event(f"Error en consulta: {exc}", "ERROR")

    def cmd_help(self, payload: Optional[str]) -> None:
        help_text = """
Portal Pi — Comandos disponibles:
  /extract_entities       Extraer entidades y relaciones de noticias raw
  /synthesize_news        Generar informe ejecutivo consolidado
  /classify_topic         Clasificar noticia en categorías temáticas
  /generate_action_items  Generar tareas accionables desde síntesis
  /run_pipeline           Ejecutar pipeline completo (4 pasos en secuencia)
  /audit_state            Auditar consistencia estado vs disco
  /show_state             Mostrar estado actual del sistema
  /db <tabla|stats>       Consultar base de datos (entities, relations, syntheses, classifications, actions, stats)
  /query <SQL>            Ejecutar consulta SQL SELECT directa
  /advance <STAGE>        Avanzar manualmente a una etapa del pipeline
  /help                   Mostrar esta ayuda
  /exit                   Cerrar Portal Pi
"""
        print(help_text)

    def cmd_exit(self, payload: Optional[str]) -> None:
        self.running = False
        log_event("Cerrando Portal Pi.")

    def run(self) -> None:
        log_event("Portal Pi iniciado. Esperando comandos.")
        while self.running:
            try:
                user_input = input("portal-pi> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                self.cmd_exit(None)
                break

            if not user_input:
                continue

            parts = user_input.split(maxsplit=1)
            cmd = parts[0]
            payload = parts[1] if len(parts) > 1 else None

            handler = self.commands.get(cmd)
            if handler:
                handler(payload)
            else:
                print(f"Comando desconocido: {cmd}. Escribe /help para ver los disponibles.")


# ─── SYSTEM PROMPTS CENTRALIZADOS DEL PIPELINE ───────────────────────────────
# Un único source of truth para los system prompts de cada paso.
# Usados por dashboard.py, scheduler.py y cualquier otro orquestador.

PIPELINE_SYSTEM_PROMPTS = {
    "extract_entities": (
        "Eres el motor de extraccion de Portal Pi. "
        "Extrae entidades y relaciones del texto. "
        "Responde SOLO en JSON valido con el schema: "
        '{"status": "ok", "data": {"source_file": "<filename>", '
        '"output_filename": "entities.json", "entities": [{"name": "<nombre>", '
        '"type": "PERSON|ORGANIZATION|LOCATION|TECHNOLOGY|EVENT|CONCEPT", '
        '"confidence": 0.0-1.0, "mentions": []}], "relations": [{"subject": "", '
        '"predicate": "", "object": ""}]}, "audit_note": ""}. '
        "No uses la clave 'noticias'."
    ),
    "classify_topic": (
        "Eres el clasificador tematico de Portal Pi. "
        "Clasifica la noticia en categorias. "
        "Responde SOLO en JSON valido con el schema: "
        '{"status": "ok", "data": {"primary_category": "<categoria>", '
        '"secondary_tags": ["tag1","tag2"], "justification": "<razon>", '
        '"source_file": ""}, "audit_note": ""}. '
        "No uses la clave 'noticias'."
    ),
    "synthesize_news": (
        "Eres el motor de sintesis de Portal Pi. "
        "Genera un informe ejecutivo. "
        "Responde SOLO en JSON valido con el schema: "
        '{"status": "ok", "data": {"executive_summary": "<resumen de 3-5 frases>", '
        '"priority": "ALTA|MEDIA|BAJA", "trends": ["t1","t2"], '
        '"source_files": [], "output_filename": "synthesis.json"}, '
        '"audit_note": ""}. '
        "No uses la clave 'noticias'."
    ),
    "generate_action_items": (
        "Eres el generador de acciones de Portal Pi. "
        "Genera items accionables. "
        "Responde SOLO en JSON valido con el schema: "
        '{"status": "ok", "data": {"action_items": [{"id": "ACT-001", '
        '"description": "", "owner": "", "deadline": "", '
        '"priority": "ALTA|MEDIA|BAJA"}], "output_filename": "actions.json", '
        '"source_file": ""}, "audit_note": ""}. '
        "No uses la clave 'noticias'."
    ),
}


# ─── PIPELINE SIMPLE REUTILIZABLE ──────────────────────────────────────────────

def run_simple_pipeline(
    llm,
    state_mgr: StateManager,
    on_step: Optional[callable] = None,
) -> List[Dict[str, Any]]:
    """
    Ejecuta el pipeline simple de 4 pasos usando el LLM.

    Args:
        llm: Instancia de LLMClient con metodo call_json(system, prompt).
        state_mgr: Instancia de StateManager.
        on_step: Callback opcional llamado con (step_name) antes de cada paso.
                 Se usa para actualizar el estado del progress bar en dashboard/scheduler.

    Returns:
        Lista de dicts con {"step": str, "status": str, "reason"?: str, "report"?: str}.

    Cada paso se ejecuta de forma aislada: si un paso falla tras reintentar, se
    marca como ``error`` (con el mensaje) y el pipeline continúa con el resto de
    pasos. De este modo un fallo transitorio de un proveedor LLM (cuota, red,...)
    nunca aborta todo el pipeline, y el informe final (paso 5) siempre se
    intenta generar con lo que se haya podido producir hasta ese momento.
    """
    import time
    from scripts.llm_client import LLMClientError

    # Reintentos para pasos LLM: opción C (reintenta y, si no, sigue).
    STEP_MAX_ATTEMPTS = 3
    STEP_RETRY_DELAY = 2.0  # segundos entre intentos

    def _run_llm_step(command: str, system_key: str, step_name: str) -> Dict[str, Any]:
        """Ejecuta un paso LLM con reintentos. Devuelve un dict de resultado."""
        last_err = ""
        for attempt in range(1, STEP_MAX_ATTEMPTS + 1):
            try:
                state = state_mgr.get_full_state()
                prompt = assemble_dynamic_prompt(state, command)
                system = PIPELINE_SYSTEM_PROMPTS[system_key]
                response = llm.call_json(system, prompt)
                route_response(state_mgr, command, response)
                log_event(f"Paso '{step_name}' OK (intento {attempt}/{STEP_MAX_ATTEMPTS})")
                return {"step": system_key, "status": "ok",
                        "attempts": attempt if attempt > 1 else None}
            except LLMClientError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                log_event(f"Paso '{step_name}' fallo LLM (intento {attempt}/{STEP_MAX_ATTEMPTS}): {last_err}", "WARN")
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                log_event(f"Paso '{step_name}' fallo (intento {attempt}/{STEP_MAX_ATTEMPTS}): {last_err}", "WARN")
            # Esperar antes del siguiente intento (salvo en el último)
            if attempt < STEP_MAX_ATTEMPTS:
                time.sleep(STEP_RETRY_DELAY)
        log_event(f"Paso '{step_name}' DEFINITIVO fallo tras {STEP_MAX_ATTEMPTS} intentos: {last_err}", "ERROR")
        return {"step": system_key, "status": "error", "reason": last_err,
                "attempts": STEP_MAX_ATTEMPTS}

    results: List[Dict[str, Any]] = []

    # ── Paso 1: Extraer entidades ──
    if on_step:
        on_step("extract_entities")
    raw_files = read_raw_news_files(limit=10)
    if raw_files:
        results.append(_run_llm_step("/extract_entities", "extract_entities", "Extraer entidades"))
        if results[-1].get("attempts") is None:
            results[-1].pop("attempts", None)
        # Marcar los archivos procesados para que no se repitan
        processed_filenames = [rf["filename"] for rf in raw_files]
        n_marked = db.mark_raw_news_processed(processed_filenames)
        if n_marked:
            log_event(f"{n_marked} archivos marcados como procesados")
    else:
        results.append({"step": "extract_entities", "status": "skipped", "reason": "sin articulos raw"})

    # ── Paso 2: Clasificar ──
    if on_step:
        on_step("classify_topic")
    r2 = _run_llm_step("/classify_topic", "classify_topic", "Clasificar")
    if r2.get("attempts") is None:
        r2.pop("attempts", None)
    results.append(r2)

    # ── Paso 3: Sintetizar ──
    if on_step:
        on_step("synthesize_news")
    r3 = _run_llm_step("/synthesize_news", "synthesize_news", "Sintetizar")
    if r3.get("attempts") is None:
        r3.pop("attempts", None)
    results.append(r3)

    # ── Paso 4: Action items ──
    if on_step:
        on_step("generate_action_items")
    r4 = _run_llm_step("/generate_action_items", "generate_action_items", "Action items")
    if r4.get("attempts") is None:
        r4.pop("attempts", None)
    results.append(r4)

    # ── Paso 5: Informe legible (siempre se intenta, con lo que haya) ──
    if on_step:
        on_step("generate_report")
    try:
        from scripts.report_generator import generate_report
        report_path = generate_report()
        results.append({"step": "generate_report", "status": "ok", "report": report_path.name})
    except Exception as exc:
        results.append({"step": "generate_report", "status": "error", "reason": str(exc)})

    return results


def main() -> None:
    cli = PortalPiCLI()
    cli.run()


if __name__ == "__main__":
    main()
