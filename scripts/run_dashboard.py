"""
run_dashboard.py
Lanza el dashboard de Portal Pi.
Uso: python scripts/run_dashboard.py [--port 8420]
"""

import sys
import argparse
from pathlib import Path

# Añadir el directorio raíz del proyecto al path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Portal Pi Dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8420, help="Port (default: 8420)")
    args = parser.parse_args()

    # Pre-flight
    for d in ["static", "templates", "data", "data/raw_news", "logs", "config"]:
        (PROJECT_ROOT / d).mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 50)
    print("  Portal Pi Dashboard")
    print(f"  http://localhost:{args.port}")
    print("=" * 50)
    print()

    # Importar aquí después de que sys.path esté configurado
    from scripts.dashboard import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
