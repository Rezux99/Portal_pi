"""
state_manager.py
Módulo de gestión de estado persistente para Portal Pi.
Opera exclusivamente sobre JSON en disco. Sin estado en RAM.
Sync a Supabase system_state table cuando está configurado.
"""

import json
import os
import shutil
import hashlib
from filelock import FileLock
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
from dataclasses import dataclass, asdict


@dataclass
class ExecutionPointers:
    current_raw_file: Optional[str] = None
    current_pipeline_stage: str = "IDLE"
    current_micro_task: Optional[str] = None
    last_completed_task: Optional[str] = None


@dataclass
class SessionMeta:
    session_id: str = "sess_000"
    started_at: str = ""
    last_activity: str = ""
    orchestrator_version: str = "1.0.0"


class StateManagerError(Exception):
    """Raised on unrecoverable state corruption or I/O lock timeout."""
    pass


class StateManager:
    """
    Gestor de estado estrictamente stateless en RAM.
    Cada operación lee/escribe desde/hacia el disco.
    Implementa bloqueo de archivo para concurrencia.
    """

    def __init__(self, state_path: str) -> None:
        self.state_path: Path = Path(state_path)
        self.backup_path: Path = self.state_path.with_suffix(".json.bak")
        self._lock: FileLock = FileLock(str(self.state_path) + ".lock")
        self._ensure_state_file()

    def _use_supabase(self) -> bool:
        from scripts.supabase_client import use_supabase
        return use_supabase()

    def _sync_to_supabase(self, data: Dict[str, Any]) -> None:
        """Sync state to Supabase system_state table."""
        if not self._use_supabase():
            return
        try:
            from scripts.supabase_database import SupabaseDatabase
            db = SupabaseDatabase()
            db.set_state("pipeline_state", data)
        except Exception:
            pass

    def _ensure_state_file(self) -> None:
        if not self.state_path.exists():
            default_state = {
                "schema_version": "1.0.0",
                "session": {
                    "session_id": "sess_000",
                    "started_at": datetime.utcnow().isoformat() + "Z",
                    "last_activity": datetime.utcnow().isoformat() + "Z",
                    "orchestrator_version": "1.0.0"
                },
                "execution_pointers": {
                    "current_raw_file": None,
                    "current_pipeline_stage": "IDLE",
                    "current_micro_task": None,
                    "last_completed_task": None
                },
                "pipeline_stages": {
                    "IDLE": {"next": "EXTRACTION", "description": "Esperando comando de inicio"},
                    "EXTRACTION": {"next": "SYNTHESIS", "description": "Extrayendo entidades"},
                    "SYNTHESIS": {"next": "AUDIT", "description": "Sintetizando"},
                    "AUDIT": {"next": "ARCHIVE", "description": "Auditando"},
                    "ARCHIVE": {"next": "IDLE", "description": "Archivando"},
                    "ERROR": {"next": "IDLE", "description": "Estado de error"}
                },
                "file_registry": {
                    "raw_news": {"directory": "data/raw_news", "files": {}, "processed_checksums": []},
                    "processed": {"directory": "data/processed", "files": {}},
                    "synthesized": {"directory": "data/synthesized", "files": {}}
                },
                "flags": {
                    "global_status": "PENDING",
                    "allowed_transitions": ["EXTRACTION", "SYNTHESIS", "AUDIT", "ARCHIVE"]
                },
                "audit_trail": []
            }
            self._atomic_write(default_state)

    def _atomic_write(self, data: Dict[str, Any]) -> None:
        """Escribe de forma atómica: temp -> backup -> overwrite."""
        temp_path = self.state_path.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            if self.state_path.exists():
                shutil.copy2(self.state_path, self.backup_path)
            os.replace(temp_path, self.state_path)
        except (OSError, IOError, json.JSONDecodeError) as exc:
            raise StateManagerError(f"Fallo de escritura atómica: {exc}") from exc
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def _read_state(self) -> Dict[str, Any]:
        """Lee el JSON de estado con bloqueo de archivo."""
        try:
            with self._lock:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    raw = f.read()
                    if not raw.strip():
                        raise StateManagerError("Archivo de estado vacío.")
                    return json.loads(raw)
        except (OSError, IOError, json.JSONDecodeError) as exc:
            raise StateManagerError(f"Fallo de lectura de estado: {exc}") from exc

    def _write_state(self, data: Dict[str, Any]) -> None:
        """Escribe el JSON de estado con bloqueo exclusivo."""
        try:
            with self._lock:
                with open(self.state_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
        except (OSError, IOError) as exc:
            raise StateManagerError(f"Fallo de escritura con lock: {exc}") from exc
        self._sync_to_supabase(data)

    def get_full_state(self) -> Dict[str, Any]:
        return self._read_state()

    def patch_state(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """
        Aplica un parche parcial al estado y persiste.
        El parche se fusiona recursivamente en el nivel superior.
        """
        state = self._read_state()

        def _deep_update(base: Dict[str, Any], updates: Dict[str, Any]) -> None:
            for key, value in updates.items():
                if isinstance(value, dict) and key in base and isinstance(base[key], dict):
                    _deep_update(base[key], value)
                else:
                    base[key] = value

        _deep_update(state, patch)
        state["session"]["last_activity"] = datetime.utcnow().isoformat() + "Z"
        self._write_state(state)
        return state

    def set_execution_pointer(self, key: str, value: Any) -> Dict[str, Any]:
        return self.patch_state({"execution_pointers": {key: value}})

    def set_flag(self, key: str, value: Any) -> Dict[str, Any]:
        return self.patch_state({"flags": {key: value}})

    def register_file(self, registry_key: str, filename: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        return self.patch_state({
            "file_registry": {
                registry_key: {
                    "files": {filename: metadata}
                }
            }
        })

    def append_audit(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        state = self._read_state()
        entry["timestamp"] = datetime.utcnow().isoformat() + "Z"
        state.setdefault("audit_trail", []).append(entry)
        self._write_state(state)
        return state

    def transition_stage(self, target_stage: str) -> Dict[str, Any]:
        state = self._read_state()
        current = state["execution_pointers"]["current_pipeline_stage"]
        allowed = state["pipeline_stages"].get(current, {}).get("next")
        allowed_list = state["flags"].get("allowed_transitions", [])

        if target_stage not in allowed_list:
            raise StateManagerError(f"Transición no permitida: {target_stage} no está en allowed_transitions.")
        if allowed and target_stage != allowed and target_stage != "ERROR":
            raise StateManagerError(f"Transición inválida: {current} -> {target_stage}. Esperado: {allowed}")

        state["execution_pointers"]["current_pipeline_stage"] = target_stage
        state["session"]["last_activity"] = datetime.utcnow().isoformat() + "Z"
        self._write_state(state)
        return state

    def checksum_file(self, filepath: Path) -> str:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
