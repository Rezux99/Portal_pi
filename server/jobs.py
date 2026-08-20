"""
jobs.py — Tareas programadas (ingesta periódica, etc.).
Usa APScheduler para ejecutar la ingesta en background.
"""

from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Scheduler setup ─────────────────────────────────────────────────────

_scheduler = None


def start_scheduler(interval_minutes: int = 30) -> None:
    """Inicia el scheduler para ingesta periódica."""
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from server.services import ops_service

        _scheduler = BackgroundScheduler()

        def _ingest_job():
            logger.info("Ingesta programada iniciada...")
            try:
                result = ops_service.run_ingest()
                logger.info(f"Ingesta programada: {result.message}")
            except Exception as e:
                logger.error(f"Error en ingesta programada: {e}")

        _scheduler.add_job(_ingest_job, "interval", minutes=interval_minutes, id="ingest")
        _scheduler.start()
        logger.info(f"Scheduler iniciado — ingesta cada {interval_minutes} min")
    except ImportError:
        logger.warning("APScheduler no instalado — ingesta automática deshabilitada")
    except Exception as e:
        logger.error(f"Error iniciando scheduler: {e}")


def stop_scheduler() -> None:
    """Detiene el scheduler."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler detenido")