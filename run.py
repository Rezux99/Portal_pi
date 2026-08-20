"""
run.py — Punto de entrada para Portal Pi v2.
Arranca uvicorn con la app FastAPI.
"""

from __future__ import annotations
import argparse
import logging
import sys
from pathlib import Path

# Asegurar que la raíz del proyecto está en sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description="Portal Pi v2 — Servidor")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Puerto (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Hot reload (dev)")
    parser.add_argument("--ingest-interval", type=int, default=0,
                        help="Intervalo en minutos para ingesta automática (0 = deshabilitado)")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Iniciar scheduler si se solicita
    if args.ingest_interval > 0:
        from server.jobs import start_scheduler
        start_scheduler(interval_minutes=args.ingest_interval)

    # Arrancar servidor
    import uvicorn
    logger = logging.getLogger("portal_pi")
    logger.info(f"Arrancando Portal Pi v2 en http://{args.host}:{args.port}")
    if args.reload:
        logger.info("Modo desarrollo con hot reload activado")

    uvicorn.run(
        "server.app:app" if args.reload else "server.app:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=not args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()