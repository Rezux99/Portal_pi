"""
dashboard.py
Dashboard web para Portal Pi — Todo autocontenido, sin StaticFiles.
"""

import json
import threading
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, Request, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from scripts.database import PortalDatabase
from scripts.state_manager import StateManager
from scripts.ingester import FeedIngester
from scripts.llm_client import LLMClient, LLMClientError, parse_json_response
from scripts.scheduler import PipelineScheduler
from scripts.report_generator import generate_report, list_reports, read_report, _load_latest_json
from scripts.multi_agent import MultiAgentOrchestrator
from scripts.timeline import feed_from_pipeline, rebuild_index, get_timeline_summary

# Orquestador de convergencia controlada
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orquestrator"))
from portal_pi_orchestrator import Orchestrator, ProviderAdapter, UnwrapAdapter


from scripts.paths import (
    BASE_DIR, STATE_PATH, DB_PATH, RAW_DIR, ENTITIES_DIR, CLASSIFIED_DIR,
    SYNTHESIZED_DIR, ACTION_ITEMS_DIR, TIMELINE_DIR, ENTITIES_TIMELINE_DIR,
    LOGS_DIR, TEMPLATES_DIR, STATIC_DIR, CONFIG_DIR, FEEDS_CONFIG_PATH,
    LLM_CONFIG_PATH, CREDENTIALS_PATH, REPORTS_DIR,
    ORCHESTRATOR_LOG, INGESTER_LOG, LLM_LOG, SCHEDULER_LOG,
)

from scripts.web_search import needs_web_search, extract_search_query, web_search
from scripts.synergy_router import is_non_empty, always_valid
from scripts.supabase_client import use_supabase, get_supabase_admin


# ─── APP ────────────────────────────────────────────────────────────────────

app = FastAPI(title="Portal Pi", version="1.0.0")

db = PortalDatabase(str(DB_PATH))
state_mgr = StateManager(str(STATE_PATH))
ingester = FeedIngester()
scheduler = PipelineScheduler()

# LLM - se inicializa bajo demanda
_llm_client: Optional[LLMClient] = None
_pipeline_lock = threading.Lock()
_pipeline_status: Dict[str, Any] = {"running": False, "started_at": None, "step": None, "results": None, "error": None}

_ingest_lock = threading.Lock()
_ingest_status: Dict[str, Any] = {"running": False, "started_at": None, "results": None, "error": None}


def _get_llm() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    else:
        # Siempre recargar config del disco por si cambió
        _llm_client._load_config()
    return _llm_client


def _get_llm_for_user(request: Request) -> LLMClient:
    """Retorna un LLMClient con credenciales del usuario (Supabase) o el singleton local."""
    user_id = _get_user_id_from_request(request)
    supa_db = _get_supabase_db()
    if user_id and supa_db:
        # Crear instancia fresh por request para no contaminar el singleton
        try:
            llm = LLMClient()
            user_creds = supa_db.list_user_credentials(user_id)
            cred_map = {c["provider"]: c["api_key"] for c in user_creds}
            if cred_map:
                llm.set_user_credentials(cred_map)
            return llm
        except LLMClientError:
            pass
    return _get_llm()


# ─── HELPERS ────────────────────────────────────────────────────────────────

def _read_last_log(log_path: Path, max_lines: int = 50) -> List[str]:
    if not log_path.exists():
        return []
    with open(log_path, "r", encoding="utf-8") as f:
        return f.readlines()[-max_lines:]


def _get_user_id_from_request(request: Request) -> Optional[str]:
    """Extrae el user_id de Supabase del header Authorization.
    Retorna None si no hay auth o está en modo local."""
    if not use_supabase():
        return None
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    try:
        admin = get_supabase_admin()
        if admin is None:
            return None
        resp = admin.auth.get_user(token)
        return resp.user.id if resp and resp.user else None
    except Exception:
        return None


def _get_supabase_db():
    """Retorna el cliente admin de Supabase para operaciones de credenciales."""
    if not use_supabase():
        return None
    try:
        admin = get_supabase_admin()
        if admin is None:
            return None
        from scripts.supabase_database import SupabaseDatabase
        return SupabaseDatabase(admin)
    except Exception:
        return None


def _run_ingest_background(feed_name: Optional[str] = None) -> None:
    global _ingest_status
    with _ingest_lock:
        if _ingest_status["running"]:
            return
        _ingest_status = {"running": True, "started_at": datetime.now(timezone.utc).isoformat(), "results": None, "error": None}
    try:
        if feed_name:
            r = ingester.ingest_feed(feed_name)
            _ingest_status["results"] = [] if r is None else [vars(r)]
            if r is None:
                _ingest_status["error"] = f"Feed '{feed_name}' no encontrado"
        else:
            _ingest_status["results"] = [vars(r) for r in ingester.ingest_all()]
    except Exception as exc:
        _ingest_status["error"] = str(exc)
    finally:
        _ingest_status["running"] = False


# ═══════════════════════════════════════════════════════════════════════════
# SERVIR ARCHIVOS ESTÁTICOS DIRECTAMENTE (sin StaticFiles mount)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/static/style.css", tags=["static"])
async def serve_css():
    p = STATIC_DIR / "style.css"
    if p.exists():
        return HTMLResponse(content=p.read_text(encoding="utf-8"), media_type="text/css")
    return HTMLResponse(content="/* not found */", media_type="text/css", status_code=404)


@app.get("/static/app.js", tags=["static"])
async def serve_js():
    p = STATIC_DIR / "app.js"
    if p.exists():
        return HTMLResponse(content=p.read_text(encoding="utf-8"), media_type="application/javascript")
    return HTMLResponse(content="// not found", media_type="application/javascript", status_code=404)


# ═══════════════════════════════════════════════════════════════════════════
# PÁGINA PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def page_dashboard():
    p = TEMPLATES_DIR / "dashboard.html"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return "<h1>Template not found</h1>"


# ═══════════════════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/status")
async def api_status():
    try:
        state = state_mgr.get_full_state()
        # Info LLM (sin exponer keys)
        llm_info = {}
        try:
            llm = _get_llm()
            llm_info = llm.get_config_info()
        except:
            pass
        return {
            "pipeline_stage": state["execution_pointers"]["current_pipeline_stage"],
            "global_status": state["flags"]["global_status"],
            "last_task": state["execution_pointers"].get("last_completed_task"),
            "db_counts": db.stats(),
            "ingester": ingester.stats(),
            "ingest_running": _ingest_status["running"],
            "llm": llm_info,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"pipeline_stage": "ERROR", "global_status": "ERROR", "db_counts": {}, "ingester": {}, "ingest_running": False, "error": str(e)}


@app.get("/api/overview")
async def api_overview():
    """Endpoint optimizado para el tab Overview: todo en una sola llamada."""
    try:
        state = state_mgr.get_full_state()
    except Exception:
        state = {"execution_pointers": {}, "flags": {}}

    db_counts = db.stats()
    ing_stats = ingester.stats()

    # ── Noticias recientes (título + fuente + fecha) ──
    recent_news = []
    try:
        files = sorted(RAW_DIR.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
        for fp in files[:12]:
            try:
                content = fp.read_text(encoding="utf-8")
                parsed = _parse_raw_article(content)
                recent_news.append({
                    "filename": fp.name,
                    "title": parsed.get("title", "") or fp.stem.replace("_", " "),
                    "source": parsed.get("source", ""),
                    "category": parsed.get("category", ""),
                    "published": parsed.get("published", ""),
                    "link_type": parsed.get("link_type", "none"),
                    "effective_link": parsed.get("effective_link", ""),
                })
            except Exception:
                pass
    except Exception:
        pass

    # ── Últimas síntesis (no solo una) ──
    recent_syntheses = []
    try:
        recent_syntheses = db.list_syntheses(limit=5)
    except Exception:
        pass

    # ── Clasificaciones recientes ──
    recent_classifications = []
    try:
        recent_classifications = db.list_classifications(limit=3)
    except Exception:
        pass

    # ── Acciones pendientes ──
    recent_actions = []
    try:
        recent_actions = db.list_action_items(limit=8)
    except Exception:
        pass

    # ── Top entidades (agrupadas por tipo, no nombres sueltos) ──
    entity_summary = {"total": 0, "by_type": {}, "top_names": []}
    try:
        all_entities = db.list_entities(limit=200)
        entity_summary["total"] = len(all_entities)
        by_type: Dict[str, int] = {}
        top_names = []
        for e in all_entities:
            etype = e.get("type", "OTRO")
            by_type[etype] = by_type.get(etype, 0) + 1
            if e.get("name") and e.get("confidence", 0) >= 0.5:
                top_names.append({
                    "name": e["name"],
                    "type": etype,
                    "confidence": e.get("confidence", 0),
                })
        entity_summary["by_type"] = by_type
        entity_summary["top_names"] = sorted(top_names, key=lambda x: x["confidence" or 0], reverse=True)[:10]
    except Exception:
        pass

    # ── Router/Sinergia ──
    router_status = {"smart_router": None, "synergy_router": None}
    try:
        llm = _get_llm()
        if llm._router:
            router_status["smart_router"] = llm._router.get_routing_status()
        if llm._synergy:
            router_status["synergy_router"] = llm._synergy.get_synergy_stats()
    except Exception:
        pass

    # ── Pipeline status ──
    pipeline_info = {
        "stage": state.get("execution_pointers", {}).get("current_pipeline_stage", "IDLE"),
        "global_status": state.get("flags", {}).get("global_status", "UNKNOWN"),
        "simple_running": _pipeline_status.get("running", False),
        "multi_agent_running": _ma_pipeline_status.get("running", False),
        "orchestrated_running": _orch_pipeline_status.get("running", False),
    }

    return {
        "db_counts": db_counts,
        "ingester": ing_stats,
        "recent_news": recent_news,
        "recent_syntheses": recent_syntheses,
        "recent_classifications": recent_classifications,
        "recent_actions": recent_actions,
        "entity_summary": entity_summary,
        "router_status": router_status,
        "pipeline": pipeline_info,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/entities")
async def api_entities(limit: int = Query(100, ge=1, le=500)):
    try: return db.list_entities(limit=limit)
    except: return []


@app.get("/api/entities/search")
async def api_entities_search(name: str = Query(..., min_length=1)):
    try: return db.search_entities(name)
    except: return []


@app.get("/api/relations")
async def api_relations(limit: int = Query(100, ge=1, le=500)):
    try: return db.list_relations(limit=limit)
    except: return []


@app.get("/api/syntheses")
async def api_syntheses(limit: int = Query(50, ge=1, le=200)):
    try: return db.list_syntheses(limit=limit)
    except: return []


@app.get("/api/classifications")
async def api_classifications(limit: int = Query(100, ge=1, le=500)):
    try: return db.list_classifications(limit=limit)
    except: return []


@app.get("/api/action_items")
async def api_action_items(limit: int = Query(100, ge=1, le=500)):
    try: return db.list_action_items(limit=limit)
    except: return []


def _parse_raw_article(content: str) -> dict:
    """Parsea un archivo raw_news y extrae campos estructurados."""
    import re
    parsed = {
        "title": "", "source": "", "category": "",
        "link": "", "link_resolved": "", "link_type": "",
        "published": "", "ingested_at": "", "body": ""
    }
    lines = content.split("\n")
    body_start = 0
    for i, line in enumerate(lines):
        m = re.match(r'^T[ÍI]TULO:\s*(.+)', line)
        if m: parsed["title"] = m.group(1).strip()
        m = re.match(r'^FUENTE:\s*(.+)', line)
        if m: parsed["source"] = m.group(1).strip()
        m = re.match(r'^CATEGOR[ÍI]A:\s*(.+)', line)
        if m: parsed["category"] = m.group(1).strip()
        m = re.match(r'^ENLACE_RESUELTO:\s*(.+)', line)
        if m: parsed["link_resolved"] = m.group(1).strip()
        m = re.match(r'^ENLACE:\s*(.+)', line)
        if m: parsed["link"] = m.group(1).strip()
        m = re.match(r'^TIPO_ENLACE:\s*(.+)', line)
        if m: parsed["link_type"] = m.group(1).strip()
        m = re.match(r'^FECHA_PUBLICACIÓN:\s*(.+)', line)
        if m: parsed["published"] = m.group(1).strip()
        m = re.match(r'^FECHA_INGESTA:\s*(.+)', line)
        if m: parsed["ingested_at"] = m.group(1).strip()
        # El cuerpo empieza después de la primera línea vacía tras los metadatos
        if line.strip() == "" and body_start == 0 and i > 0:
            body_start = i + 1
    if body_start > 0:
        parsed["body"] = "\n".join(lines[body_start:]).strip()
    else:
        parsed["body"] = content

    # Determinar link_type si no está en el archivo (artículos antiguos)
    if not parsed["link_type"]:
        link = parsed["link_resolved"] or parsed["link"]
        if not link or not link.startswith('http'):
            parsed["link_type"] = "none"
        elif 'news.google.com' in link:
            parsed["link_type"] = "indirect"
        else:
            parsed["link_type"] = "direct"
    # El enlace útil es el resuelto si existe
    parsed["effective_link"] = parsed["link_resolved"] or parsed["link"]
    return parsed


@app.get("/api/raw_news")
async def api_raw_news(limit: int = Query(50, ge=1, le=200)):
    try:
        files = sorted(RAW_DIR.glob("*.txt")) + sorted(RAW_DIR.glob("*.md"))
        result = []
        for fp in files[:limit]:
            try:
                content = fp.read_text(encoding="utf-8")
                parsed = _parse_raw_article(content)
                result.append({
                    "filename": fp.name,
                    "size_bytes": fp.stat().st_size,
                    "preview": content[:500],
                    "title": parsed["title"],
                    "source": parsed["source"],
                    "category": parsed["category"],
                    "link": parsed["link"],
                    "link_resolved": parsed["link_resolved"],
                    "link_type": parsed["link_type"],
                    "effective_link": parsed["effective_link"],
                    "published": parsed["published"],
                    "ingested_at": parsed["ingested_at"],
                    "body": parsed["body"],
                })
            except: pass
        return result
    except: return []


@app.get("/api/raw_news/{filename}")
async def api_raw_news_detail(filename: str):
    """Devuelve el contenido completo de un archivo raw."""
    fp = RAW_DIR / filename
    if not fp.exists() or not fp.is_file():
        return {"status": "error", "message": "Archivo no encontrado"}
    try:
        content = fp.read_text(encoding="utf-8")
        parsed = _parse_raw_article(content)
        return {
            "filename": fp.name,
            "content": content,
            "size_bytes": fp.stat().st_size,
            "title": parsed["title"],
            "source": parsed["source"],
            "category": parsed["category"],
            "link": parsed["link"],
            "link_resolved": parsed["link_resolved"],
            "link_type": parsed["link_type"],
            "effective_link": parsed["effective_link"],
            "published": parsed["published"],
            "ingested_at": parsed["ingested_at"],
            "body": parsed["body"],
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/versions")
async def api_versions():
    """Lista todos los archivos versionados en los directorios de salida."""
    dirs = {
        "entities": ENTITIES_DIR,
        "classified": CLASSIFIED_DIR,
        "synthesized": SYNTHESIZED_DIR,
        "action_items": ACTION_ITEMS_DIR,
    }
    result = {}
    for label, d in dirs.items():
        files = []
        if d.exists():
            for fp in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    files.append({
                        "filename": fp.name,
                        "size_bytes": fp.stat().st_size,
                        "modified": datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc).isoformat(),
                        "preview": json.dumps(data, ensure_ascii=False)[:200],
                    })
                except: pass
        result[label] = files
    return result


@app.get("/api/versions/{category}/{filename}")
async def api_version_detail(category: str, filename: str):
    """Devuelve el contenido completo de un archivo versionado."""
    dirs = {
        "entities": ENTITIES_DIR,
        "classified": CLASSIFIED_DIR,
        "synthesized": SYNTHESIZED_DIR,
        "action_items": ACTION_ITEMS_DIR,
    }
    d = dirs.get(category)
    if not d:
        return {"status": "error", "message": f"Categoría '{category}' no válida"}
    fp = d / filename
    if not fp.exists():
        return {"status": "error", "message": "Archivo no encontrado"}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return {"filename": fp.name, "category": category, "content": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ─── REPORTS ENDPOINTS ────────────────────────────────────────────────

@app.get("/api/reports")
async def api_reports_list():
    """Lista los informes generados."""
    return list_reports()


@app.post("/api/reports/generate")
async def api_report_generate():
    try:
        path = generate_report()
        return {"status": "ok", "filename": path.name, "message": "Informe generado"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/reports/{filename}")
async def api_report_detail(filename: str):
    """Devuelve el contenido Markdown de un informe."""
    content = read_report(filename)
    if content is None:
        return {"status": "error", "message": "Informe no encontrado"}
    return {"filename": filename, "content": content}


@app.get("/api/feeds")
async def api_feeds():
    try: return ingester.list_feeds()
    except: return []


@app.post("/api/feeds/add")
async def api_feeds_add(request: Request):
    try: body = await request.json()
    except: return {"status": "error", "message": "JSON inválido"}
    name, url, cat = body.get("name", ""), body.get("url", ""), body.get("category", "Otro")
    if not name or not url: return {"status": "error", "message": "name y url requeridos"}
    try: return {"status": "ok", "feed": ingester.add_feed(name, url, cat)}
    except Exception as e: return {"status": "error", "message": str(e)}


@app.post("/api/feeds/toggle")
async def api_feeds_toggle(request: Request):
    try: body = await request.json()
    except: return {"status": "error", "message": "JSON inválido"}
    name = body.get("name", "")
    try:
        ns = ingester.toggle_feed(name)
        if ns is None: return {"status": "error", "message": f"Feed '{name}' no encontrado"}
        return {"status": "ok", "enabled": ns}
    except Exception as e: return {"status": "error", "message": str(e)}


@app.post("/api/ingest")
async def api_ingest(request: Request):
    if _ingest_status["running"]:
        return {"status": "already_running", "message": "Ingesta en curso..."}
    body = {}
    try: body = await request.json()
    except: pass
    feed_name = body.get("feed_name") if body else None
    threading.Thread(target=_run_ingest_background, args=(feed_name,), daemon=True).start()
    return {"status": "started", "message": "Ingesta iniciada"}


@app.get("/api/ingest/status")
async def api_ingest_status():
    return _ingest_status


@app.get("/api/state")
async def api_state():
    try: return state_mgr.get_full_state()
    except Exception as e: return {"error": str(e)}


@app.get("/api/logs/orchestrator")
async def api_orchestrator_log(lines: int = Query(50, ge=1, le=500)):
    return _read_last_log(ORCHESTRATOR_LOG, lines)


@app.get("/api/logs/ingester")
async def api_ingester_log(lines: int = Query(50, ge=1, le=500)):
    return _read_last_log(INGESTER_LOG, lines)


@app.get("/api/logs/llm")
async def api_llm_log(lines: int = Query(50, ge=1, le=500)):
    return _read_last_log(LLM_LOG, lines)


# ─── LLM ENDPOINTS ───────────────────────────────────────────────────────

@app.get("/api/llm/config")
async def api_llm_config(request: Request):
    try:
        llm = _get_llm()
        config_info = llm.get_config_info()

        # Si hay usuario autenticado, mergear keys de Supabase
        user_id = _get_user_id_from_request(request)
        supa_db = _get_supabase_db()
        if user_id and supa_db:
            user_creds = supa_db.list_user_credentials(user_id)
            user_cred_map = {c["provider"]: c["api_key"] for c in user_creds}
            for name, info in config_info.get("providers", {}).items():
                if name in user_cred_map:
                    key = user_cred_map[name]
                    masked = key[:6] + "..." + key[-4:] if len(key) > 12 else "✓"
                    info["api_key_status"] = masked
                    info["has_key"] = True

        return {"status": "ok", "config": config_info}
    except LLMClientError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/llm/test")
async def api_llm_test(request: Request):
    try:
        llm = _get_llm_for_user(request)
        results = llm.test_all()
        return {"status": "ok", "providers": [r.to_dict() for r in results]}
    except LLMClientError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/llm/router/status")
async def api_llm_router_status():
    """Estado del SmartRouter: scores, circuit breakers, telemetría."""
    try:
        llm = _get_llm()
        if llm._router:
            return {"status": "ok", "router": llm._router.get_routing_status()}
        return {"status": "error", "message": "SmartRouter no inicializado"}
    except LLMClientError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/llm/credentials")
async def api_llm_set_credential(request: Request):
    """Guarda una API key. En modo Supabase, en user_credentials; si no, en .credentials.json."""
    try:
        body = await request.json()
    except:
        return {"status": "error", "message": "JSON inválido"}

    provider = body.get("provider", "")
    api_key = body.get("api_key", "")

    if not provider:
        return {"status": "error", "message": "provider es requerido"}
    if not api_key:
        return {"status": "error", "message": "api_key es requerido"}

    try:
        llm = _get_llm()
        if provider not in llm.providers:
            return {"status": "error", "message": f"Proveedor '{provider}' no encontrado. Disponibles: {list(llm.providers.keys())}"}

        # Supabase: guardar en user_credentials (persistente)
        user_id = _get_user_id_from_request(request)
        supa_db = _get_supabase_db()
        if user_id and supa_db:
            supa_db.set_user_credential(user_id, provider, api_key)
            return {"status": "ok", "message": f"API key para '{provider}' guardada (Supabase)"}

        # Local: guardar en .credentials.json
        llm.set_credential(provider, api_key)
        return {"status": "ok", "message": f"API key para '{provider}' guardada"}
    except LLMClientError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/api/llm/credentials/{provider}")
async def api_llm_delete_credential(provider: str, request: Request):
    """Elimina una API key. En modo Supabase, de user_credentials; si no, de .credentials.json."""
    try:
        user_id = _get_user_id_from_request(request)
        supa_db = _get_supabase_db()
        if user_id and supa_db:
            supa_db.delete_user_credential(user_id, provider)
            return {"status": "ok", "message": f"API key para '{provider}' eliminada (Supabase)"}

        llm = _get_llm()
        llm.set_credential(provider, "")
        return {"status": "ok", "message": f"API key para '{provider}' eliminada"}
    except LLMClientError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ─── PIPELINE EN BACKGROUND ──────────────────────────────────────────────

def _run_pipeline_background() -> None:
    """Ejecuta el pipeline completo en background."""
    global _pipeline_status
    with _pipeline_lock:
        if _pipeline_status["running"]:
            return
        _pipeline_status = {"running": True, "started_at": datetime.now(timezone.utc).isoformat(), "step": "starting", "results": None, "error": None}

    try:
        llm = _get_llm()

        def on_step(step_name: str) -> None:
            _pipeline_status["step"] = step_name

        results = run_simple_pipeline(llm, state_mgr, on_step=on_step)
        _pipeline_status["results"] = results

    except LLMClientError as exc:
        _pipeline_status["error"] = str(exc)
    except Exception as exc:
        _pipeline_status["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _pipeline_status["running"] = False
        _pipeline_status["step"] = "done"


@app.post("/api/pipeline/run")
async def api_pipeline_run():
    """Lanza el pipeline completo con LLM en background."""
    if _pipeline_status["running"]:
        return {"status": "already_running", "message": "Pipeline en curso..."}
    try:
        _get_llm()  # Verificar que el LLM está configurado
    except LLMClientError as e:
        return {"status": "error", "message": str(e)}
    threading.Thread(target=_run_pipeline_background, daemon=True).start()
    return {"status": "started", "message": "Pipeline iniciado"}


@app.get("/api/pipeline/status")
async def api_pipeline_status():
    return _pipeline_status


# ─── SCHEDULER ENDPOINTS ──────────────────────────────────────────────

@app.get("/api/scheduler/status")
async def api_scheduler_status():
    try:
        return scheduler.status()
    except Exception as e:
        return {"running": False, "error": str(e)}


@app.post("/api/scheduler/start")
async def api_scheduler_start():
    try:
        # Verificar que hay LLM configurado antes de arrancar
        if scheduler.get_settings().get("auto_pipeline", True):
            try:
                _get_llm()
            except LLMClientError as e:
                return {"status": "error", "message": f"LLM no configurado: {e}"}
        return scheduler.start()
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/scheduler/stop")
async def api_scheduler_stop():
    try:
        return scheduler.stop()
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/scheduler/settings")
async def api_scheduler_settings(request: Request):
    try:
        body = await request.json()
    except:
        return {"status": "error", "message": "JSON inválido"}
    try:
        updated = scheduler.update_settings(body)
        return {"status": "ok", "settings": updated}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/logs/scheduler")
async def api_scheduler_log(lines: int = Query(50, ge=1, le=500)):
    return _read_last_log(SCHEDULER_LOG, lines)


# ─── MULTI-AGENT PIPELINE ────────────────────────────────────────────

_ma_pipeline_status: Dict[str, Any] = {"running": False, "started_at": None, "results": None, "error": None}


def _run_multi_agent_background() -> None:
    """Ejecuta el pipeline multi-agente en background."""
    global _ma_pipeline_status
    _ma_pipeline_status = {"running": True, "started_at": datetime.now(timezone.utc).isoformat(), "results": None, "error": None}
    try:
        llm = _get_llm()
        orchestrator = MultiAgentOrchestrator(llm_client=llm)
        results = orchestrator.run_pipeline(state_mgr, db)

        # Alimentar la timeline con los resultados
        try:
            entities_data = classified_data = synthesis_data = critique_data = None
            # Recoger datos de los archivos versionados más recientes
            entities_data = _load_latest_json(ENTITIES_DIR)
            classified_data = _load_latest_json(CLASSIFIED_DIR)
            synthesis_data = _load_latest_json(SYNTHESIZED_DIR)
            # Crítica está en results
            critique_data = results.get("critique", {}).get("data")

            feed_result = feed_from_pipeline(
                entities_data=entities_data,
                classified_data=classified_data,
                synthesis_data=synthesis_data,
                critique_data=critique_data,
                source_files=None,
            )
            results["timeline"] = feed_result
            # Reconstruir índice
            rebuild_index()
        except Exception as exc:
            results["timeline_error"] = str(exc)

        _ma_pipeline_status["results"] = results

    except LLMClientError as exc:
        _ma_pipeline_status["error"] = str(exc)
    except Exception as exc:
        _ma_pipeline_status["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _ma_pipeline_status["running"] = False


@app.post("/api/pipeline/multi-agent")
async def api_multi_agent_run():
    """Lanza el pipeline multi-agente en background."""
    if _ma_pipeline_status["running"]:
        return {"status": "already_running", "message": "Pipeline multi-agente en curso..."}
    try:
        _get_llm()
    except LLMClientError as e:
        return {"status": "error", "message": str(e)}
    threading.Thread(target=_run_multi_agent_background, daemon=True).start()
    return {"status": "started", "message": "Pipeline multi-agente iniciado"}


@app.get("/api/pipeline/multi-agent/status")
async def api_multi_agent_status():
    return _ma_pipeline_status


# ─── ORCHESTRATED PIPELINE (Convergencia Controlada) ───────────────────────

_orch_pipeline_status: Dict[str, Any] = {"running": False, "started_at": None, "results": None, "error": None}


def _prepare_orchestrator_inputs(limit: int = 5) -> List[Dict[str, Any]]:
    """Lee raw news y los prepara como inputs para el Orchestrator.
    Prioriza artículos con fuente accesible (link_type=direct)."""
    files = sorted(RAW_DIR.glob("*.txt")) + sorted(RAW_DIR.glob("*.md"))
    all_articles = []
    for fp in files:
        try:
            content = fp.read_text(encoding="utf-8")
            parsed = _parse_raw_article(content)
            title = parsed["title"] or fp.name
            source = parsed["source"] or ""
            link = parsed["effective_link"] or parsed["link"] or ""
            body = parsed["body"] or ""
            link_type = parsed["link_type"] or "none"
            # Truncar body a 800 chars para no exceder context
            body_truncated = body[:800] + ("..." if len(body) > 800 else "")
            all_articles.append({
                "id": fp.stem,
                "text": f"TÍTULO: {title}\nFUENTE: {source}\nENLACE: {link}\n\n{body_truncated}",
                "source": link or source,
                "link_type": link_type,
                "priority": 0 if link_type == "direct" else 1,
            })
        except Exception:
            pass
    # Priorizar artículos con fuente accesible
    all_articles.sort(key=lambda a: a["priority"])
    # Quitar campos auxiliares antes de enviar al orquestador
    for a in all_articles:
        a.pop("link_type", None)
        a.pop("priority", None)
    return all_articles[:limit]


def _run_orchestrated_pipeline_background() -> None:
    """Ejecuta el pipeline orquestado con convergencia controlada."""
    global _orch_pipeline_status
    _orch_pipeline_status = {
        "running": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "results": None,
        "error": None,
        "step": "preparing",
    }

    try:
        llm = _get_llm()

        # ── Preparar inputs (priorizar fuentes accesibles, truncar para no exceder context) ──
        _orch_pipeline_status["step"] = "preparing"
        inputs = _prepare_orchestrator_inputs(limit=5)
        if not inputs:
            _orch_pipeline_status["error"] = "No hay artículos raw para procesar"
            _orch_pipeline_status["running"] = False
            return

        # ── Crear adaptador que unwrapa el wrapper {status, data} ──
        adapter = ProviderAdapter(
            call_json=UnwrapAdapter(llm.call_json),
            name="portal-pi-llm",
        )

        # ── Ejecutar orquestador ──
        _orch_pipeline_status["step"] = "planning"
        objective = (
            "Analiza las noticias proporcionadas y extrae conclusiones accionables. "
            "Identifica entidades clave (personas, organizaciones, tecnologías), "
            "sus relaciones, la categoría temática principal, la prioridad del conjunto, "
            "y genera acciones concretas. Sé riguroso: cada afirmación debe estar "
            "respaldada por la evidencia. No inventes datos."
        )
        orchestrator = Orchestrator(adapter, max_rounds=2, min_quality=0.75, max_claims=15)
        result = orchestrator.run(objective=objective, inputs=inputs)

        # ── Convertir resultado a formato Portal Pi ──
        _orch_pipeline_status["step"] = "saving"
        orch_dict = result.to_dict()

        # Extraer claims como entidades
        entities = []
        relations = []
        for claim in result.claims:
            if claim.text and len(claim.text) > 3:
                entities.append({
                    "name": claim.text[:200],
                    "type": "CONCEPT",
                    "confidence": claim.confidence,
                    "mentions": claim.evidence_ids,
                })

        # Guardar entidades en DB si hay
        if entities:
            try:
                n = db.insert_entities(entities, "orchestrated-pipeline")
                orch_dict["entities_saved"] = n
            except Exception as exc:
                orch_dict["entities_save_error"] = str(exc)

        # Guardar síntesis en DB si hay answer
        if result.answer:
            try:
                synth_data = {
                    "executive_summary": result.answer,
                    "priority": "ALTA" if result.quality.get("score", 0) >= 0.8 else "MEDIA",
                    "trends": result.contradictions[:5] if result.contradictions else [],
                    "source_files": [inp.get("id", "") for inp in inputs[:5]],
                    "output_filename": "orchestrated_synthesis.json",
                }
                db.insert_synthesis(synth_data)
                orch_dict["synthesis_saved"] = True
            except Exception as exc:
                orch_dict["synthesis_save_error"] = str(exc)

        # Guardar action items
        if result.next_actions:
            try:
                items = []
                for i, action in enumerate(result.next_actions[:10]):
                    items.append({
                        "id": f"ORCH-{i+1:03d}",
                        "description": str(action),
                        "owner": "",
                        "deadline": "",
                        "priority": "ALTA" if i < 2 else "MEDIA",
                    })
                db.insert_action_items(items, "orchestrated-pipeline")
                orch_dict["actions_saved"] = len(items)
            except Exception as exc:
                orch_dict["actions_save_error"] = str(exc)

        # Guardar resultado completo en disco
        try:
            from scripts.main import _versioned_filename
            out_name = _versioned_filename("orchestrated_result.json")
            out_path = SYNTHESIZED_DIR / out_name
            out_path.write_text(
                json.dumps(orch_dict, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            orch_dict["output_file"] = out_name
        except Exception as exc:
            orch_dict["output_save_error"] = str(exc)

        # Alimentar timeline
        try:
            feed_from_pipeline(
                entities_data={"entities": entities},
                synthesis_data={"executive_summary": result.answer, "priority": "ALTA"},
                source_files=[inp.get("id", "") for inp in inputs[:5]],
            )
            rebuild_index()
        except Exception as exc:
            orch_dict["timeline_error"] = str(exc)

        # Generar informe
        try:
            report_path = generate_report()
            orch_dict["report"] = report_path.name
        except Exception as exc:
            orch_dict["report_error"] = str(exc)

        _orch_pipeline_status["results"] = orch_dict

    except LLMClientError as exc:
        _orch_pipeline_status["error"] = str(exc)
    except Exception as exc:
        _orch_pipeline_status["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _orch_pipeline_status["running"] = False
        _orch_pipeline_status["step"] = "done"


@app.post("/api/pipeline/orchestrated")
async def api_orchestrated_pipeline_run():
    """Lanza el pipeline orquestado con convergencia controlada."""
    if _orch_pipeline_status["running"]:
        return {"status": "already_running", "message": "Pipeline orquestado en curso..."}
    try:
        _get_llm()
    except LLMClientError as e:
        return {"status": "error", "message": str(e)}
    threading.Thread(target=_run_orchestrated_pipeline_background, daemon=True).start()
    return {"status": "started", "message": "Pipeline orquestado iniciado"}


@app.get("/api/pipeline/orchestrated/status")
async def api_orchestrated_pipeline_status():
    return _orch_pipeline_status




# ─── HYBRID ORCHESTRATOR (Async Multi-Provider) ────────────────────────

import httpx as _httpx

_HYBRID_URL = "http://127.0.0.1:8787"
_HYBRID_SECRET = ""

_hybrid_status: Dict[str, Any] = {"running": False, "started_at": None, "results": None, "error": None}

# Try to load the hybrid orchestrator secret from .env
try:
    _env_path = BASE_DIR / "hybrid_orchestrator" / ".env"
    if _env_path.exists():
        for _line in _env_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line.startswith("ORCHESTRATOR_API_SECRET="):
                _HYBRID_SECRET = _line.split("=", 1)[1].strip()
                break
except Exception:
    pass


@app.get("/api/hybrid/health")
async def api_hybrid_health():
    """Check if the hybrid orchestrator service is running."""
    try:
        async with _httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{_HYBRID_URL}/health")
            return r.json()
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc)[:200], "url": _HYBRID_URL}


@app.get("/api/hybrid/providers")
async def api_hybrid_providers():
    """List hybrid orchestrator providers (requires secret)."""
    if not _HYBRID_SECRET:
        return {"status": "error", "message": "ORCHESTRATOR_API_SECRET no configurado en hybrid_orchestrator/.env"}
    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_HYBRID_URL}/v1/providers",
                headers={"X-Orchestrator-Secret": _HYBRID_SECRET},
            )
            return r.json()
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:300]}


@app.post("/api/hybrid/run")
async def api_hybrid_run(request: Request):
    """Run the hybrid orchestrator pipeline (async multi-provider).
    Sends evidence to the hybrid orchestrator for parallel multi-provider analysis.
    """
    global _hybrid_status
    if _hybrid_status["running"]:
        return {"status": "already_running", "message": "Hybrid pipeline en curso..."}
    if not _HYBRID_SECRET:
        return {"status": "error", "message": "ORCHESTRATOR_API_SECRET no configurado en hybrid_orchestrator/.env"}

    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        body = {}

    mode = body.get("mode", "balanced")
    max_rounds = body.get("max_rounds", 2)
    limit = body.get("limit", 5)
    dry_run = body.get("dry_run", False)

    # Prepare inputs from raw news
    inputs = _prepare_orchestrator_inputs(limit=limit)
    if not inputs:
        return {"status": "error", "message": "No hay articulos raw para procesar"}

    objective = body.get("objective", (
        "Analiza las noticias proporcionadas y extrae conclusiones accionables. "
        "Identifica entidades clave, sus relaciones, la categoria tematica principal, "
        "la prioridad del conjunto, y genera acciones concretas. "
        "Se riguroso: cada afirmacion debe estar respaldada por la evidencia. No inventes datos."
    ))

    evidence = [
        {"id": inp.get("id", f"e-{i}"), "source": inp.get("source", ""), "text": inp.get("text", "")}
        for i, inp in enumerate(inputs)
    ]

    _hybrid_status = {
        "running": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "results": None,
        "error": None,
        "step": "calling_hybrid",
    }

    # Run in background thread
    def _run_hybrid():
        global _hybrid_status
        try:
            import httpx as sync_httpx
            payload = {
                "objective": objective,
                "mode": mode,
                "max_rounds": max_rounds,
                "dry_run": dry_run,
                "evidence": evidence,
            }
            with sync_httpx.Client(timeout=180) as client:
                r = client.post(
                    f"{_HYBRID_URL}/v1/run",
                    headers={
                        "X-Orchestrator-Secret": _HYBRID_SECRET,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                r.raise_for_status()
                result = r.json()

            # Save result
            _hybrid_status["step"] = "saving"

            # Extract entities from claims
            entities = []
            for claim in result.get("claims", []):
                if claim.get("text") and len(claim.get("text", "")) > 3:
                    entities.append({
                        "name": claim["text"][:200],
                        "type": "CONCEPT",
                        "confidence": claim.get("confidence", 0),
                        "mentions": claim.get("evidence_ids", []),
                    })

            if entities:
                try:
                    db.insert_entities(entities, "hybrid-orchestrator")
                    result["entities_saved"] = len(entities)
                except Exception as exc:
                    result["entities_save_error"] = str(exc)

            # Save synthesis
            if result.get("answer"):
                try:
                    synth_data = {
                        "executive_summary": result["answer"],
                        "priority": "ALTA" if result.get("quality", {}).get("score", 0) >= 0.8 else "MEDIA",
                        "trends": result.get("contradictions", [])[:5],
                        "source_files": [e.get("id", "") for e in evidence[:5]],
                        "output_filename": "hybrid_orchestrated_synthesis.json",
                    }
                    db.insert_synthesis(synth_data)
                    result["synthesis_saved"] = True
                except Exception as exc:
                    result["synthesis_save_error"] = str(exc)

            # Save action items
            if result.get("next_actions"):
                try:
                    items = []
                    for i, action in enumerate(result["next_actions"][:10]):
                        items.append({
                            "id": f"HYB-{i+1:03d}",
                            "description": str(action),
                            "owner": "",
                            "deadline": "",
                            "priority": "ALTA" if i < 2 else "MEDIA",
                        })
                    db.insert_action_items(items, "hybrid-orchestrator")
                    result["actions_saved"] = len(items)
                except Exception as exc:
                    result["actions_save_error"] = str(exc)

            # Save full result to disk
            try:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                out_name = f"hybrid_result_{ts}.json"
                out_path = SYNTHESIZED_DIR / out_name
                out_path.write_text(
                    json.dumps(result, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
                result["output_file"] = out_name
            except Exception as exc:
                result["output_save_error"] = str(exc)

            # Feed timeline
            try:
                feed_from_pipeline(
                    entities_data={"entities": entities},
                    synthesis_data={"executive_summary": result.get("answer", ""), "priority": "ALTA"},
                    source_files=[e.get("id", "") for e in evidence[:5]],
                )
                rebuild_index()
            except Exception as exc:
                result["timeline_error"] = str(exc)

            # Generate report
            try:
                report_path = generate_report()
                result["report"] = report_path.name
            except Exception as exc:
                result["report_error"] = str(exc)

            _hybrid_status["results"] = result

        except Exception as exc:
            _hybrid_status["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _hybrid_status["running"] = False
            _hybrid_status["step"] = "done"

    threading.Thread(target=_run_hybrid, daemon=True).start()
    return {"status": "started", "message": "Hybrid orchestrator pipeline iniciado", "evidence_count": len(evidence)}


@app.get("/api/hybrid/status")
async def api_hybrid_status():
    """Status of the hybrid orchestrator pipeline run."""
    return _hybrid_status


# ─── TIMELINE ENDPOINTS ─────────────────────────────────────────────

@app.get("/api/timeline/summary")
async def api_timeline_summary():
    try:
        return get_timeline_summary()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/timeline/month/{year}/{filename}")
async def api_timeline_month(year: str, filename: str):
    """Devuelve el contenido de un archivo mensual de timeline."""
    fp = TIMELINE_DIR / year / filename
    if not fp.exists():
        return {"status": "error", "message": "No encontrado"}
    try:
        return {"content": fp.read_text(encoding="utf-8"), "filename": filename}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/timeline/entity/{entity_name}")
async def api_timeline_entity(entity_name: str):
    """Devuelve el perfil de una entidad."""
    fp = ENTITIES_TIMELINE_DIR / f"{entity_name}.md"
    if not fp.exists():
        return {"status": "error", "message": "Entidad no encontrada"}
    try:
        return {"content": fp.read_text(encoding="utf-8"), "entity": entity_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/timeline/rebuild-index")
async def api_timeline_rebuild():
    try:
        path = rebuild_index()
        return {"status": "ok", "path": str(path)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.on_event("startup")
async def startup():
    # Auto-arrancar scheduler si está habilitado en config
    settings = scheduler.get_settings()
    if settings.get("enabled", False):
        try:
            _get_llm()  # Verificar LLM
            scheduler.start()
            print("  Scheduler auto-iniciado (enabled=true en config)")
        except LLMClientError:
            print("  Scheduler NO auto-iniciado: LLM sin configurar")
        except Exception as e:
            print(f"  Scheduler NO auto-iniciado: {e}")

    print("\n" + "=" * 50)
    print("  Portal Pi Dashboard — LISTO")
    print("  http://localhost:8420")
    print("=" * 50 + "\n")


# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET CHAT — Streaming en tiempo real con Modal
# ═══════════════════════════════════════════════════════════════════════════

# Conexiones WebSocket activas
_active_ws_clients: List[WebSocket] = []


# ─── CONTEXT BUILDER — Datos reales para el chat ───────────────────────────

def _build_chat_context(user_message: str) -> str:
    """
    Construye un contexto con datos REALES del sistema + búsqueda web
    para inyectar en el system prompt del chat.

    Orden de prioridad:
    1. Si la pregunta necesita datos actuales → búsqueda web en tiempo real
    2. Datos del sistema (noticias raw, síntesis, entidades, etc.)
    """
    parts = []
    msg_lower = user_message.lower()

    # ── Determinar qué datos son relevantes según la pregunta ──
    wants_news = any(kw in msg_lower for kw in [
        "novedad", "noticia", "qué pasa", "que pasa", "actualidad", "reciente",
        "último", "ultimo", "nuevo", "nueva", "hoy", "ayer", "esta semana",
        "qué hay", "que hay", "resumen", "informe", "síntesis", "sintesis",
        "news", "latest", "what's happening", "current",
    ])
    wants_entities = any(kw in msg_lower for kw in [
        "entidad", "organización", "organizacion", "persona", "empresa", "tecnología",
        "quién", "quien", "who", "entity", "entities",
    ])
    wants_actions = any(kw in msg_lower for kw in [
        "acción", "accion", "tarea", "pendiente", "action", "task", "todo",
        "hacer", "seguimiento", "follow",
    ])
    wants_all = not wants_news and not wants_entities and not wants_actions

    # ── BÚSQUEDA WEB (si la pregunta lo necesita) ──
    if needs_web_search(user_message):
        try:
            # Construir query inteligente usando entidades del sistema
            entity_names = []
            try:
                top_entities = db.list_entities(limit=5)
                entity_names = [e.get("name", "") for e in top_entities if e.get("name")]
            except Exception:
                pass

            categories = []
            try:
                classifications = db.list_classifications(limit=2)
                categories = [c.get("primary_category", "") for c in classifications if c.get("primary_category")]
            except Exception:
                pass

            query = extract_search_query(user_message, entities=entity_names)
            if categories:
                cat_str = " ".join(categories[:2])
                if cat_str.lower() not in query.lower():
                    query = f"{query} {cat_str}"

            search_resp = web_search(query, max_results=8)

            if search_resp.results:
                web_context = search_resp.to_context_text(max_results=8)
                parts.append(
                    f"🌐 RESULTADOS DE BÚSQUEDA WEB EN TIEMPO REAL (query: '{query}', "
                    f"{search_resp.total_found} resultados, {search_resp.elapsed_sec}s):\n\n"
                    f"{web_context}\n\n"
                    f"⚠️ IMPORTANTE: Estos son resultados de búsqueda web. Cita la fuente y la URL "
                    f"cuando uses esta información. Si los resultados no son relevantes para la "
                    f"pregunta, ignóralos y usa solo los datos del sistema."
                )
            else:
                parts.append(
                    f"🌐 BÚSQUEDA WEB: No se encontraron resultados para '{query}'. "
                    f"Responde solo con lo que tengas en el sistema."
                )
        except Exception as exc:
            # Si la búsqueda falla, no bloquear el chat
            parts.append(f"🌐 Búsqueda web no disponible: {exc}")

    # ── Siempre incluir noticias recientes del sistema (es lo principal) ──
    if wants_news or wants_all:
        try:
            # Leer los artículos raw más recientes
            files = sorted(RAW_DIR.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
            articles = []
            for fp in files[:15]:
                try:
                    content = fp.read_text(encoding="utf-8")
                    parsed = _parse_raw_article(content)
                    title = parsed.get("title", "") or fp.stem
                    source = parsed.get("source", "")
                    category = parsed.get("category", "")
                    published = parsed.get("published", "")
                    body = parsed.get("body", "")
                    link = parsed.get("effective_link", "") or parsed.get("link", "")
                    body_preview = body[:300] + ("..." if len(body) > 300 else "") if body else ""
                    articles.append(
                        f"- [{source}] {title}"
                        + (f" ({category})" if category else "")
                        + (f" — {published}" if published else "")
                        + (f"\n  Resumen: {body_preview}" if body_preview else "")
                        + (f"\n  Enlace: {link}" if link else "")
                    )
                except Exception:
                    continue
            if articles:
                parts.append(f"📰 NOTICIAS RECIENTES EN EL SISTEMA ({len(articles)} artículos):\n" + "\n".join(articles))
            else:
                parts.append("📰 NO HAY NOTICIAS RAW en el sistema. Si el usuario pregunta por noticias y no hay resultados web, dile que ejecute una ingesta desde el tab Feeds o Pipeline.")
        except Exception as exc:
            parts.append(f"📰 Error leyendo noticias: {exc}")

    # ── Síntesis recientes ──
    if wants_news or wants_all:
        try:
            syntheses = db.list_syntheses(limit=3)
            if syntheses:
                synth_lines = []
                for s in syntheses:
                    summary = s.get("executive_summary", "")
                    priority = s.get("priority", "")
                    date = s.get("created_at", "")
                    trends = s.get("trends", "[]")
                    if isinstance(trends, str):
                        try:
                            trends = json.loads(trends)
                        except:
                            trends = []
                    synth_lines.append(
                        f"- [{priority}] {summary[:300]}"
                        + (f" (Tendencias: {', '.join(trends[:5])})" if trends else "")
                        + (f" — {date}" if date else "")
                    )
                parts.append("📊 SÍNTESIS EJECUTIVAS RECIENTES:\n" + "\n".join(synth_lines))
        except Exception:
            pass

    # ── Entidades recientes ──
    if wants_entities or wants_all:
        try:
            entities = db.list_entities(limit=15)
            if entities:
                ent_lines = []
                for e in entities:
                    name = e.get("name", "")
                    etype = e.get("type", "")
                    confidence = e.get("confidence", 0)
                    ent_lines.append(f"- {name} ({etype}, confianza: {confidence})")
                parts.append("🏷️ ENTIDADES IDENTIFICADAS RECIENTEMENTE:\n" + "\n".join(ent_lines))
        except Exception:
            pass

    # ── Clasificaciones recientes ──
    if wants_all:
        try:
            classifications = db.list_classifications(limit=5)
            if classifications:
                class_lines = []
                for c in classifications:
                    cat = c.get("primary_category", "")
                    tags = c.get("secondary_tags", "[]")
                    if isinstance(tags, str):
                        try:
                            tags = json.loads(tags)
                        except:
                            tags = []
                    just = c.get("justification", "")
                    class_lines.append(f"- {cat} (tags: {', '.join(tags[:3])})" + (f": {just[:150]}" if just else ""))
                parts.append("📂 CLASIFICACIONES RECIENTES:\n" + "\n".join(class_lines))
        except Exception:
            pass

    # ── Action items ──
    if wants_actions or wants_all:
        try:
            actions = db.list_action_items(limit=10)
            if actions:
                act_lines = []
                for a in actions:
                    desc = a.get("description", "")
                    priority = a.get("priority", "")
                    owner = a.get("owner", "")
                    act_lines.append(f"- [{priority}] {desc}" + (f" (owner: {owner})" if owner else ""))
                parts.append("✅ ACCIONES PENDIENTES:\n" + "\n".join(act_lines))
        except Exception:
            pass

    if not parts:
        return ""

    return "\n\n".join(parts)


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    """
    WebSocket para chat streaming en tiempo real.
    
    Protocolo:
    - Cliente envía: {"action": "chat", "message": "...", "provider": "modal"}
    - Servidor responde: {"type": "token", "content": "..."}  (por cada token)
    - Servidor responde: {"type": "done", "full_text": "...", "provider": "...", "model": "..."}
    - Servidor responde: {"type": "error", "message": "..."}
    - También soporta: {"action": "test", "provider": "modal"}
    """
    await websocket.accept()
    _active_ws_clients.append(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "JSON inválido"})
                continue

            action = data.get("action", "chat")
            provider_name = data.get("provider", "modal")

            if action == "test":
                # Test de conexión al proveedor
                try:
                    llm = _get_llm()
                    if provider_name not in llm.providers:
                        await websocket.send_json({"type": "error", "message": f"Proveedor '{provider_name}' no encontrado"})
                        continue
                    # Test rápido
                    result = llm.call("Responde solo: OK", "Test de conexión")
                    await websocket.send_json({
                        "type": "test_result",
                        "provider": provider_name,
                        "status": "ok",
                        "response": result[:100],
                        "model": llm.providers[provider_name].get("model", ""),
                    })
                except Exception as exc:
                    await websocket.send_json({
                        "type": "test_result",
                        "provider": provider_name,
                        "status": "error",
                        "message": str(exc)[:200],
                    })
                continue

            if action == "search":
                # ── Búsqueda web directa ──
                search_query = data.get("query", "") or message
                if not search_query:
                    await websocket.send_json({"type": "error", "message": "Query vacía"})
                    continue
                try:
                    loop = asyncio.get_event_loop()
                    search_resp = await loop.run_in_executor(None, web_search, search_query)
                    results_list = []
                    for r in search_resp.results[:8]:
                        results_list.append({
                            "title": r.title,
                            "url": r.url,
                            "snippet": r.snippet,
                            "source": r.source,
                            "date": r.date,
                        })
                    await websocket.send_json({
                        "type": "search_results",
                        "query": search_query,
                        "results": results_list,
                        "total": search_resp.total_found,
                        "elapsed_sec": search_resp.elapsed_sec,
                    })
                except Exception as exc:
                    await websocket.send_json({"type": "error", "message": f"Búsqueda falló: {exc}"})
                continue

            if action == "chat":
                message = data.get("message", "")
                if not message:
                    await websocket.send_json({"type": "error", "message": "Mensaje vacío"})
                    continue

                # ── Construir system prompt con datos REALES del sistema + web ──
                real_context = _build_chat_context(message)
                system_prompt = (
                    "Eres el asistente de inteligencia de Portal Pi. "
                    "Respondes SIEMPRE en español, de forma clara y concisa.\n\n"
                    "REGLAS CRÍTICAS:\n"
                    "1. SOLO puedes afirmar datos que estén en el CONTEXTO real a continuación.\n"
                    "2. Si el contexto incluye RESULTADOS DE BÚSQUEDA WEB, puedes usar esa información "
                    "citando la fuente y el enlace.\n"
                    "3. Si el contexto no tiene información relevante, di EXPLÍCITAMENTE que no tienes "
"datos sobre eso.\n"
                    "4. NUNCA inventes noticias, fechas, nombres, cifras ni eventos.\n"
                    "5. Si no estás seguro, di que no tienes certeza. La veracidad es tu prioridad #1.\n"
                    "6. Si usas datos de búsqueda web, incluye el enlace de la fuente.\n\n"
                    "CONTEXTO REAL DEL SISTEMA (datos verificados + búsqueda web si aplica):\n"
                    + (real_context if real_context else "⚠️ No hay datos en el sistema ni resultados web. "
                       "Si el usuario pregunta por noticias, sugiérele que ejecute una ingesta "
                       "o usa el comando /buscar para buscar en la web.")
                )

                try:
                    llm = _get_llm()

                    # ── MODO SINÉRGICO: usar SmartRouter + SynergyRouter ──
                    # Si el usuario no fuerza un proveedor ("auto" o no especifica),
                    # el sistema enruta automáticamente: rápido → failover si cae → streaming.
                    use_synergy = data.get("mode", "auto") == "auto" or provider_name == "auto"

                    if use_synergy and llm._router:
                        # ── Flujo sinérgico con streaming real ──
                        # 1. SmartRouter elige el mejor proveedor por score
                        # 2. Streaming de tokens desde ese proveedor
                        # 3. Si falla, failover automático al siguiente proveedor
                        # 4. Validación is_non_empty: rechazar respuestas basura
                        try:
                            loop = asyncio.get_event_loop()
                            full_text = ""
                            final_metadata = {}

                            # El generador produce tuplas (token, metadata)
                            for token, meta in llm.call_with_synergy_stream(system_prompt, message):
                                if meta.get("type") == "routing":
                                    # Informar al frontend de qué proveedor se eligió
                                    await websocket.send_json({
                                        "type": "routing",
                                        "provider": meta["provider"],
                                        "model": meta.get("model", ""),
                                        "attempt": meta.get("attempt", 1),
                                        "total_available": meta.get("total_available", 1),
                                    })
                                elif meta.get("type") == "token":
                                    # Token de streaming — enviar al frontend en tiempo real
                                    full_text += token
                                    await websocket.send_json({"type": "token", "content": token})
                                elif meta.get("type") == "failover":
                                    # Un proveedor falló — informar y seguir intentando
                                    await websocket.send_json({
                                        "type": "failover",
                                        "failed_provider": meta["failed_provider"],
                                        "error": meta.get("error", ""),
                                        "trying_next": meta.get("trying_next", False),
                                    })
                                elif meta.get("type") == "done":
                                    final_metadata = meta

                            # Validación post-stream: si la respuesta es basura, intentar corrección
                            if not is_non_empty(full_text) and llm._synergy:
                                # Sinergia: pedir a un proveedor corrector que lo arregle
                                await websocket.send_json({
                                    "type": "routing",
                                    "provider": "correction",
                                    "model": "synergy_correction",
                                    "reason": "Respuesta del proveedor rápido no pasó validación, corrigiendo...",
                                })
                                try:
                                    result = await loop.run_in_executor(
                                        None,
                                        lambda: llm.call_with_synergy(
                                            system_prompt, message,
                                            validator=always_valid,
                                        )
                                    )
                                    full_text = result.value
                                    final_metadata["synergy_used"] = True
                                    final_metadata["synergy_phase"] = result.phase
                                    final_metadata["correction_provider"] = result.provider
                                except Exception:
                                    pass  # La corrección falló, devolver lo que tenemos

                            # Enviar done con metadata rica
                            await websocket.send_json({
                                "type": "done",
                                "provider": final_metadata.get("provider", "unknown"),
                                "model": final_metadata.get("model", ""),
                                "transport": "synergy",
                                "output_chars": len(full_text),
                                "failed_providers": final_metadata.get("failed_providers", []),
                                "synergy_used": final_metadata.get("synergy_used", False),
                                "synergy_phase": final_metadata.get("synergy_phase", "draft"),
                            })

                        except LLMClientError as exc:
                            await websocket.send_json({"type": "error", "message": str(exc)[:300]})
                        except Exception as exc:
                            await websocket.send_json({"type": "error", "message": f"{type(exc).__name__}: {str(exc)[:200]}"})

                    else:
                        # ── Modo directo: proveedor específico forzado por el usuario ──
                        pcfg = llm.providers.get(provider_name)
                        if not pcfg:
                            await websocket.send_json({"type": "error", "message": f"Proveedor '{provider_name}' no encontrado"})
                            continue

                        supports_ws = pcfg.get("websocket_url", "")

                        if supports_ws:
                            # ── Streaming via WebSocket directo ──
                            try:
                                async for token in llm.call_websocket_stream(system_prompt, message, provider_name):
                                    await websocket.send_json({"type": "token", "content": token})
                                await websocket.send_json({
                                    "type": "done",
                                    "provider": provider_name,
                                    "model": pcfg.get("model", ""),
                                    "transport": "websocket",
                                })
                            except Exception as exc:
                                await websocket.send_json({"type": "error", "message": str(exc)[:300]})
                        else:
                            # ── Streaming via SSE (OpenAI SDK) ──
                            try:
                                loop = asyncio.get_event_loop()
                                full_text = ""
                                for token in llm.call_stream(system_prompt, message, provider_name):
                                    full_text += token
                                    await websocket.send_json({"type": "token", "content": token})
                                await websocket.send_json({
                                    "type": "done",
                                    "provider": provider_name,
                                    "model": pcfg.get("model", ""),
                                    "transport": "sse",
                                })
                            except Exception as exc:
                                await websocket.send_json({"type": "error", "message": str(exc)[:300]})

                except LLMClientError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)[:300]})
                except Exception as exc:
                    await websocket.send_json({"type": "error", "message": f"{type(exc).__name__}: {str(exc)[:200]}"})
                continue

            # Acción desconocida
            await websocket.send_json({"type": "error", "message": f"Acción '{action}' no reconocida"})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"WebSocket error: {exc}")
    finally:
        if websocket in _active_ws_clients:
            _active_ws_clients.remove(websocket)


# ── Chat REST (non-streaming fallback) ──────────────────────────────────

@app.post("/api/chat")
async def api_chat(request: Request):
    """Chat REST (non-streaming). Para clientes sin WebSocket."""
    try:
        body = await request.json()
    except:
        return {"status": "error", "message": "JSON inválido"}
    
    message = body.get("message", "")
    provider = body.get("provider", "modal")
    
    if not message:
        return {"status": "error", "message": "Mensaje vacío"}
    
    # ── Construir system prompt con datos REALES del sistema + web ──
    real_context = _build_chat_context(message)
    system_prompt = (
        "Eres el asistente de inteligencia de Portal Pi. "
        "Respondes SIEMPRE en español, de forma clara y concisa.\n\n"
        "REGLAS CRÍTICAS:\n"
        "1. SOLO puedes afirmar datos que estén en el CONTEXTO real a continuación.\n"
        "2. Si el contexto incluye RESULTADOS DE BÚSQUEDA WEB, puedes usar esa información "
        "citando la fuente y el enlace.\n"
        "3. Si el contexto no tiene información relevante, di EXPLÍCITAMENTE que no tienes "
"datos sobre eso.\n"
        "4. NUNCA inventes noticias, fechas, nombres, cifras ni eventos.\n"
        "5. Si no estás seguro, di que no tienes certeza. La veracidad es tu prioridad #1.\n"
        "6. Si usas datos de búsqueda web, incluye el enlace de la fuente.\n\n"
        "CONTEXTO REAL DEL SISTEMA (datos verificados + búsqueda web si aplica):\n"
        + (real_context if real_context else "⚠️ No hay datos en el sistema ni resultados web. "
           "Si el usuario pregunta por noticias, sugiérele que ejecute una ingesta "
           "o usa el comando /buscar para buscar en la web.")
    )
    
    try:
        llm = _get_llm_for_user(request)
        # Usar sinergia si está disponible (validación + failover automático)
        use_synergy = body.get("mode", "auto") == "auto" or provider == "auto"
        if use_synergy and llm._synergy:
            result = llm.call_with_synergy(system_prompt, message, validator=is_non_empty)
            return {
                "status": "ok",
                "response": result.value,
                "provider": result.provider,
                "model": llm.providers.get(result.provider, {}).get("model", ""),
                "synergy_used": result.synergy_used,
                "synergy_phase": result.phase,
                "validated": result.validated,
                "total_latency_ms": round(result.total_latency_ms, 0),
            }
        else:
            response = llm.call(system_prompt, message)
            return {
                "status": "ok",
                "response": response,
                "provider": llm._preferred_provider,
                "model": llm.providers.get(llm._preferred_provider, {}).get("model", ""),
            }
    except LLMClientError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/web-search")
async def api_web_search(request: Request):
    """Búsqueda web directa. Devuelve resultados sin pasar por el LLM."""
    try:
        body = await request.json()
    except:
        return {"status": "error", "message": "JSON inválido"}
    
    query = body.get("query", "")
    max_results = body.get("max_results", 10)
    
    if not query:
        return {"status": "error", "message": "query es requerido"}
    
    try:
        search_resp = web_search(query, max_results=max_results)
        return {
            "status": "ok",
            "query": search_resp.query,
            "results": [
                {
                    "title": r.title,
                    "url": r.url,
                    "snippet": r.snippet,
                    "source": r.source,
                    "date": r.date,
                }
                for r in search_resp.results
            ],
            "total": search_resp.total_found,
            "elapsed_sec": search_resp.elapsed_sec,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
