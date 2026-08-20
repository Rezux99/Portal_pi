"""
scheduler.py
Scheduler en background para Portal Pi.
Ingesta periódica de feeds + pipeline automático configurable.
Usa threading.Event para shutdown limpio — sin dependencias extra.
"""

import json
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field, asdict


from scripts.paths import BASE_DIR, LOGS_DIR, FEEDS_CONFIG_PATH, STATE_PATH, SCHEDULER_LOG, log_to_file, load_json_config, save_json_config

CONFIG_PATH = FEEDS_CONFIG_PATH


@dataclass
class SchedulerRun:
    """Registro de una ejecución del scheduler."""
    started_at: str = ""
    ingest_result: Optional[Dict[str, Any]] = None
    pipeline_result: Optional[Dict[str, Any]] = None
    status: str = "pending"  # pending, running, ok, error
    error: Optional[str] = None
    finished_at: str = ""


class PipelineScheduler:
    """
    Scheduler que ejecuta ingesta + pipeline periódicamente.

    Configuración en config/feeds.json → settings.scheduler:
    {
        "enabled": false,
        "ingest_interval_min": 30,
        "auto_pipeline": true
    }

    - enabled: si true, arranca automáticamente con el dashboard
    - ingest_interval_min: minutos entre ingestas
    - auto_pipeline: si true, ejecuta el pipeline después de cada ingesta
    """

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._last_run: Optional[SchedulerRun] = None
        self._run_history: List[SchedulerRun] = []
        self._next_run_at: Optional[str] = None
        self._current_step: Optional[str] = None
        self._total_runs: int = 0

    # ─── LOGGING ────────────────────────────────────────────────────────

    def _log(self, message: str, level: str = "INFO") -> None:
        log_to_file(SCHEDULER_LOG, message, level, prefix="[SCHEDULER]")

    # ─── CONFIG ─────────────────────────────────────────────────────────

    def _load_config(self) -> Dict[str, Any]:
        return load_json_config(CONFIG_PATH)

    def _save_config(self, config: Dict[str, Any]) -> None:
        save_json_config(CONFIG_PATH, config)

    def get_settings(self) -> Dict[str, Any]:
        config = self._load_config()
        defaults = {
            "enabled": False,
            "ingest_interval_min": 30,
            "auto_pipeline": True,
        }
        settings = config.get("settings", {}).get("scheduler", {})
        return {**defaults, **settings}

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        config = self._load_config()
        config.setdefault("settings", {})
        current = config["settings"].get("scheduler", {})
        current.update(updates)
        config["settings"]["scheduler"] = current
        self._save_config(config)
        self._log(f"Configuración actualizada: {updates}")
        return current

    # ─── ESTADO ─────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    def status(self) -> Dict[str, Any]:
        settings = self.get_settings()
        return {
            "running": self._running,
            "enabled": settings.get("enabled", False),
            "ingest_interval_min": settings.get("ingest_interval_min", 30),
            "auto_pipeline": settings.get("auto_pipeline", True),
            "current_step": self._current_step,
            "next_run_at": self._next_run_at,
            "total_runs": self._total_runs,
            "last_run": asdict(self._last_run) if self._last_run else None,
            "recent_runs": [
                asdict(r) for r in self._run_history[-5:]
            ] if self._run_history else [],
        }

    # ─── START / STOP ───────────────────────────────────────────────────

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self._running:
                return {"status": "already_running", "message": "Scheduler ya en ejecución"}
            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            self._log("Scheduler iniciado")
            return {"status": "started", "message": "Scheduler iniciado"}

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            if not self._running:
                return {"status": "not_running", "message": "Scheduler no está en ejecución"}
            self._stop_event.set()
            self._running = False
            self._log("Scheduler detenido (señal enviada)")
            return {"status": "stopped", "message": "Scheduler deteniéndose..."}

    # ─── BUCLE PRINCIPAL ─────────────────────────────────────────────────

    def _run_loop(self) -> None:
        settings = self.get_settings()
        interval_sec = settings.get("ingest_interval_min", 30) * 60

        self._log(f"Intervalo configurado: {settings.get('ingest_interval_min', 30)} min")
        self._next_run_at = datetime.now(timezone.utc).isoformat()

        while not self._stop_event.is_set():
            run = SchedulerRun(
                started_at=datetime.now(timezone.utc).isoformat(),
                status="running"
            )
            self._current_step = "ingesting"

            try:
                # ── Paso 1: Ingesta ──
                self._log("Iniciando ingesta programada...")
                from scripts.ingester import FeedIngester
                ingester = FeedIngester()
                ingest_results = ingester.ingest_all(only_enabled=True)
                total_new = sum(r.articles_new for r in ingest_results)
                total_skipped = sum(r.articles_skipped for r in ingest_results)
                total_errors = sum(len(r.errors) for r in ingest_results)
                run.ingest_result = {
                    "total_new": total_new,
                    "total_skipped": total_skipped,
                    "total_errors": total_errors,
                    "feeds": [asdict(r) for r in ingest_results],
                }
                self._log(f"Ingesta completada: {total_new} nuevos, {total_skipped} duplicados, {total_errors} errores")

                # ── Paso 2: Pipeline (si hay artículos nuevos y está configurado) ──
                if settings.get("auto_pipeline", True) and total_new > 0:
                    self._current_step = "pipeline"
                    self._log("Iniciando pipeline automático...")
                    pipeline_result = self._run_pipeline()
                    run.pipeline_result = pipeline_result
                    if pipeline_result.get("status") == "ok":
                        self._log("Pipeline automático completado")
                    else:
                        self._log(f"Pipeline automático falló: {pipeline_result.get('error', 'desconocido')}", "WARN")
                elif not settings.get("auto_pipeline", True):
                    self._log("Pipeline automático deshabilitado en configuración")
                else:
                    self._log("No hay artículos nuevos, pipeline omitido")

                run.status = "ok"

            except Exception as exc:
                run.status = "error"
                run.error = str(exc)
                self._log(f"Error en ejecución programada: {exc}", "ERROR")

            run.finished_at = datetime.now(timezone.utc).isoformat()
            self._last_run = run
            self._run_history.append(run)
            if len(self._run_history) > 20:
                self._run_history = self._run_history[-20:]
            self._total_runs += 1
            self._current_step = "waiting"

            # ── Calcular próxima ejecución ──
            next_dt = datetime.now(timezone.utc)
            self._next_run_at = next_dt.isoformat()

            # ── Esperar con interrupción limpia ──
            self._log(f"Próxima ejecución en {settings.get('ingest_interval_min', 30)} min")
            self._stop_event.wait(timeout=interval_sec)

        self._current_step = None
        self._next_run_at = None
        self._log("Bucle del scheduler terminado")

    # ─── EJECUCIÓN DEL PIPELINE ──────────────────────────────────────────

    def _run_pipeline(self) -> Dict[str, Any]:
        """Ejecuta el pipeline completo usando run_simple_pipeline de main.py."""
        try:
            from scripts.llm_client import LLMClient
            from scripts.main import run_simple_pipeline
            from scripts.state_manager import StateManager

            llm = LLMClient()
            state_mgr = StateManager(str(STATE_PATH))

            results = run_simple_pipeline(llm, state_mgr)

            ok_count = sum(1 for r in results if r["status"] == "ok")
            return {
                "status": "ok",
                "steps": results,
                "summary": f"{ok_count}/{len(results)} pasos completados"
            }

        except Exception as exc:
            return {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "steps": []
            }
