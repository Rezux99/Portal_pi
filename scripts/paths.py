"""
paths.py
Constantes de rutas y utilidades compartidas del proyecto Portal Pi.

Un único source of truth para:
- BASE_DIR y todos los subdirectorios
- Logging a archivo (log_to_file)
- Carga/guardado de config JSON (load_json_config, save_json_config)

Todos los módulos deben importar desde aquí en vez de redefinir.

Uso:
    from scripts.paths import BASE_DIR, DB_PATH, RAW_DIR, LOGS_DIR, log_to_file, ...
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional

BASE_DIR = Path(__file__).resolve().parent.parent

# ─── State ──────────────────────────────────────────────────────────────
STATE_PATH = BASE_DIR / "state" / "memoria_proyecto.json"

# ─── Data ───────────────────────────────────────────────────────────────
DB_PATH = BASE_DIR / "data" / "portal_pi.db"
RAW_DIR = BASE_DIR / "data" / "raw_news"
PROCESSED_DIR = BASE_DIR / "data" / "processed"
ENTITIES_DIR = BASE_DIR / "data" / "entities"
CLASSIFIED_DIR = BASE_DIR / "data" / "classified"
SYNTHESIZED_DIR = BASE_DIR / "data" / "synthesized"
ACTION_ITEMS_DIR = BASE_DIR / "data" / "action_items"
TIMELINE_DIR = BASE_DIR / "data" / "timeline"
ENTITIES_TIMELINE_DIR = TIMELINE_DIR / "entities"
REPORTS_DIR = BASE_DIR / "data" / "reports"

# ─── Config ─────────────────────────────────────────────────────────────
CONFIG_DIR = BASE_DIR / "config"
FEEDS_CONFIG_PATH = CONFIG_DIR / "feeds.json"
LLM_CONFIG_PATH = CONFIG_DIR / "llm.json"
CREDENTIALS_PATH = CONFIG_DIR / ".credentials.json"
CRED_KEY_PATH = CONFIG_DIR / ".cred_key"

# ─── Logs ───────────────────────────────────────────────────────────────
LOGS_DIR = BASE_DIR / "logs"
ORCHESTRATOR_LOG = LOGS_DIR / "orchestrator.log"
INGESTER_LOG = LOGS_DIR / "ingester.log"
LLM_LOG = LOGS_DIR / "llm.log"
SCHEDULER_LOG = LOGS_DIR / "scheduler.log"

# ─── Web ────────────────────────────────────────────────────────────────
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# ─── Ensure dirs exist ─────────────────────────────────────────────────
for _d in (RAW_DIR, PROCESSED_DIR, SYNTHESIZED_DIR, ENTITIES_DIR,
           CLASSIFIED_DIR, ACTION_ITEMS_DIR, LOGS_DIR, TIMELINE_DIR,
           REPORTS_DIR, ENTITIES_TIMELINE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ─── Logging a archivo ─────────────────────────────────────────────────
# Reemplaza los 5 métodos _log() idénticos que había dispersos.

def log_to_file(log_path: Path, message: str, level: str = "INFO", prefix: str = "") -> None:
    """
    Escribe una línea de log con timestamp ISO en el archivo dado.

    Args:
        log_path: Ruta al archivo de log (ej: ORCHESTRATOR_LOG).
        message: Mensaje a registrar.
        level: Nivel (INFO, WARN, ERROR, CRITICAL).
        prefix: Etiqueta opcional (ej: "[SCHEDULER]").
    """
    ts = datetime.now(timezone.utc).isoformat()
    tag = f" {prefix}" if prefix else ""
    line = f"[{ts}] [{level}]{tag} {message}\n"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    print(line.strip())


# ─── Carga/guardado de config JSON ─────────────────────────────────────
# Reemplaza los _load_config / _save_config duplicados de ingester y scheduler.

def load_json_config(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Carga un archivo JSON de configuración. Devuelve default si no existe."""
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else {}


def save_json_config(path: Path, data: Dict[str, Any]) -> None:
    """Persiste un dict como JSON con indentación y ensure_ascii=False."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
