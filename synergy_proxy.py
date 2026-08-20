"""
synergy_proxy.py — Portal Pi Synergy Router Proxy
====================================================
Servidor OpenAI-compatible que pi (y otros clientes) pueden usar como
si fuera un solo modelo, pero que internamente enruta con:

  1. SmartRouter: scoring por peso × latencia × éxito × circuit breaker
  2. SynergyRouter: validación + corrección automática + failover
  3. Web Search: DuckDuckGo automático cuando la query lo necesita
  4. Streaming SSE: tokens en tiempo real con metadata de routing

Uso:
  python synergy_proxy.py                    # puerto 8788
  python synergy_proxy.py --port 9000         # puerto custom

Registro en pi (~/.pi/agent/models.json):
  {
    "providers": {
      "synergy": {
        "api": "openai-completions",
        "baseUrl": "http://127.0.0.1:8788/v1",
        "apiKey": "synergy-local",
        "models": [
          { "id": "synergy/auto", "name": "Synergy Router (Auto)" }
        ]
      }
    }
  }

Endpoints:
  GET  /v1/models           — Lista el modelo virtual
  POST /v1/chat/completions  — Chat completion (streaming + non-streaming)
  GET  /health               — Estado de proveedores y circuit breakers
  GET  /stats                — Telemetría detallada del router
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ── Importar los módulos del Portal Pi ──
import sys
from pathlib import Path

# Asegurar que scripts/ está en el path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(BASE_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(BASE_DIR / "scripts"))

from scripts.llm_client import LLMClient, LLMClientError
from scripts.smart_router import SmartRouter, CircuitState
from scripts.synergy_router import (
    SynergyRouter, SynergyConfig, SynergyResult,
    is_non_empty, is_valid_json, always_valid,
)
from scripts.web_search import needs_web_search, extract_search_query, web_search

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger("synergy_proxy")


# ── INICIALIZACIÓN ──────────────────────────────────────────────────────────

def _init_llm() -> LLMClient:
    """Inicializa el LLMClient con todos los providers configurados."""
    try:
        return LLMClient()
    except Exception as e:
        logger.error(f"No se pudo inicializar LLMClient: {e}")
        raise


llm: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    global llm
    if llm is None:
        llm = _init_llm()
    return llm


# ── MODELOS PYDANTIC ───────────────────────────────────────────────────────

class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "synergy/auto"
    messages: List[Message]
    temperature: float = 0.3
    max_tokens: int = 4096
    stream: bool = False
    # Extensiones propias del proxy
    synergy_mode: str = Field(default="auto", description="auto|fast|quality|direct")
    synergy_validator: str = Field(default="non_empty", description="non_empty|json|always")
    synergy_web_search: Optional[bool] = Field(default=None, description="Force web search on/off")


class ModelObject(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str


# ── WEB SEARCH CONTEXT ─────────────────────────────────────────────────────

def _maybe_web_search(messages: List[Message], force: Optional[bool] = None) -> str:
    """
    Determina si necesita búsqueda web y la ejecuta.
    Devuelve texto de contexto para inyectar en el system prompt, o "".
    """
    # Buscar el último mensaje del usuario
    user_msg = ""
    for m in reversed(messages):
        if m.role == "user":
            user_msg = m.content
            break

    if not user_msg:
        return ""

    should_search = force if force is not None else needs_web_search(user_msg)
    if not should_search:
        return ""

    try:
        query = extract_search_query(user_msg)
        resp = web_search(query, max_results=8)
        if resp.results:
            context = resp.to_context_text(max_results=8, max_snippet=300)
            return (
                f"\n\n🌐 RESULTADOS DE BÚSQUEDA WEB (query: '{query}', "
                f"{resp.total_found} resultados, {resp.elapsed_sec}s):\n\n"
                f"{context}\n\n"
                f"⚠️ Cita la fuente y URL cuando uses esta información. "
                f"Si no es relevante, ignórala."
            )
    except Exception as e:
        logger.warning(f"Web search falló: {e}")
    return ""


# ── VALIDATOR SELECTION ────────────────────────────────────────────────────

def _get_validator(name: str):
    validators = {
        "non_empty": is_non_empty,
        "json": is_valid_json,
        "always": always_valid,
    }
    return validators.get(name, is_non_empty)


# ── STREAMING HELPERS ──────────────────────────────────────────────────────

def _sse_chunk(id: str, model: str, delta_content: Optional[str] = None,
               finish_reason: Optional[str] = None) -> str:
    """Genera un chunk SSE en formato OpenAI."""
    delta = {}
    if delta_content is not None:
        delta["content"] = delta_content
    if finish_reason:
        delta["finish_reason"] = finish_reason

    chunk = {
        "id": f"chatcmpl-{id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _sse_routing_info(id: str, provider: str, model: str, attempt: int,
                      total: int, phase: str = "draft") -> str:
    """Chunk SSE con metadata de routing (no estándar, pero útil para clientes que lo entiendan)."""
    chunk = {
        "id": f"chatcmpl-{id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "synergy_meta": {
            "provider": provider,
            "attempt": attempt,
            "total_available": total,
            "phase": phase,
        },
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


# ── FASTAPI APP ────────────────────────────────────────────────────────────

app = FastAPI(title="Synergy Router Proxy", version="1.0.0")

# Stats en memoria
_stats: Dict[str, Any] = {
    "total_requests": 0,
    "total_streaming": 0,
    "total_non_streaming": 0,
    "total_web_searches": 0,
    "total_synergy_corrections": 0,
    "total_failovers": 0,
    "provider_calls": {},  # provider -> count
    "provider_errors": {},  # provider -> count
    "started_at": time.time(),
}


def _record_call(provider: str, success: bool) -> None:
    _stats["provider_calls"][provider] = _stats["provider_calls"].get(provider, 0) + 1
    if not success:
        _stats["provider_errors"][provider] = _stats["provider_errors"].get(provider, 0) + 1


# ── ENDPOINTS ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Estado de salud del proxy y sus proveedores."""
    try:
        client = get_llm()
        available = client._available_providers()
        router_status = client._router.get_routing_status() if client._router else {}

        providers_health = {}
        for name in available:
            pcfg = client.providers.get(name, {})
            cb = client._router._breakers.get(name) if client._router else None
            providers_health[name] = {
                "model": pcfg.get("model", ""),
                "circuit_state": cb.state.value if cb else "unknown",
                "available": cb.allow_request() if cb else True,
            }

        return {
            "status": "ok",
            "providers_available": available,
            "providers_health": providers_health,
            "smart_router": router_status,
            "synergy_stats": client._synergy.get_synergy_stats() if client._synergy else {},
            "proxy_stats": {**_stats, "uptime_s": round(time.time() - _stats["started_at"], 0)},
        }
    except Exception as e:
        return {"status": "error", "message": str(e)[:300]}


@app.get("/stats")
async def stats():
    """Telemetría detallada."""
    return await health()


@app.get("/v1/models")
async def list_models():
    """OpenAI-compatible model list."""
    return {
        "object": "list",
        "data": [
            {
                "id": "synergy/auto",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "synergy-router",
                "permission": [],
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    """
    OpenAI-compatible chat completions endpoint.
    Internamente enruta con SmartRouter + SynergyRouter + Web Search.
    """
    _stats["total_requests"] += 1
    req_id = uuid.uuid4().hex[:12]
    t_start = time.perf_counter()

    try:
        client = get_llm()
    except Exception as e:
        raise HTTPException(503, f"LLMClient no disponible: {e}")

    # ── Extraer system prompt y user message ──
    system_prompt = ""
    user_message = ""
    messages_for_llm = []

    for m in request.messages:
        if m.role == "system":
            system_prompt += m.content + "\n"
        elif m.role == "user":
            user_message = m.content
        messages_for_llm.append({"role": m.role, "content": m.content})

    if not user_message:
        raise HTTPException(400, "No se encontró mensaje de usuario")

    # ── Web Search: inyectar contexto si es necesario ──
    web_context = _maybe_web_search(request.messages, force=request.synergy_web_search)
    if web_context:
        _stats["total_web_searches"] += 1
        system_prompt += web_context

    # ── Validador ──
    validator = _get_validator(request.synergy_validator)

    # ── MODO DIRECTO: un solo proveedor, sin routing ──
    if request.synergy_mode == "direct":
        available = client._available_providers()
        if not available:
            raise HTTPException(503, "No hay proveedores disponibles")
        provider = available[0]
        pcfg = client.providers.get(provider, {})
        model_name = pcfg.get("model", provider)

        if request.stream:
            _stats["total_streaming"] += 1
            return StreamingResponse(
                _stream_direct(client, system_prompt, user_message, provider, req_id, model_name),
                media_type="text/event-stream",
            )
        else:
            _stats["total_non_streaming"] += 1
            return await _non_stream_direct(client, system_prompt, user_message, provider, model_name, t_start)

    # ── MODO SINÉRGICO: SmartRouter + SynergyRouter + failover ──
    if request.stream:
        _stats["total_streaming"] += 1
        return StreamingResponse(
            _stream_synergy(client, system_prompt, user_message, validator, request, req_id),
            media_type="text/event-stream",
        )
    else:
        _stats["total_non_streaming"] += 1
        return await _non_stream_synergy(
            client, system_prompt, user_message, validator, request, req_id, t_start
        )


# ── STREAMING: MODO SINÉRGICO ──────────────────────────────────────────────

async def _stream_synergy(
    client: LLMClient,
    system_prompt: str,
    user_message: str,
    validator,
    request: ChatRequest,
    req_id: str,
):
    """
    Streaming sinérgico:
    1. SmartRouter elige proveedor por score
    2. Streaming SSE desde ese proveedor
    3. Si falla → failover automático al siguiente
    4. Si la respuesta es basura → corrección con SynergyRouter
    """
    available = client._available_providers()
    if not available:
        yield _sse_chunk(req_id, "synergy/auto", "Error: no hay proveedores disponibles", "stop")
        yield "data: [DONE]\n\n"
        return

    # SmartRouter ranking
    ranked = _rank_providers(client, available, request.synergy_mode)
    total_available = len(ranked)

    full_text = ""
    used_provider = "unknown"
    used_model = "unknown"
    failed_providers = []
    synergy_correction = False

    for attempt, (provider, score, reason) in enumerate(ranked, 1):
        pcfg = client.providers.get(provider, {})
        model_name = pcfg.get("model", provider)

        # Enviar metadata de routing al cliente
        yield _sse_routing_info(req_id, provider, model_name, attempt, total_available, "draft")

        try:
            # Intentar streaming desde este proveedor
            loop = asyncio.get_event_loop()

            def _stream_gen():
                tokens = []
                for token in client.call_stream(system_prompt, user_message, provider):
                    tokens.append(token)
                return tokens

            chunks = await loop.run_in_executor(None, _stream_gen)

            for token in chunks:
                full_text += token
                yield _sse_chunk(req_id, model_name, token)

            used_provider = provider
            used_model = model_name
            _record_call(provider, True)

            # Streaming exitoso — verificar si la respuesta es válida
            if validator != always_valid and not validator(full_text):
                # Respuesta basura → sinergia de corrección
                synergy_correction = True
                _stats["total_synergy_corrections"] += 1
                yield _sse_routing_info(req_id, "correction", "synergy", attempt, total_available, "correction")

                correction_result = await loop.run_in_executor(
                    None,
                    lambda: client.call_with_synergy(
                        system_prompt, user_message, validator=always_valid
                    ),
                )
                full_text = correction_result.value
                used_provider = correction_result.provider
                used_model = client.providers.get(used_provider, {}).get("model", used_provider)

                # Re-enviar la corrección como un chunk grande
                yield _sse_chunk(req_id, used_model, full_text, None)

            break  # Éxito — salir del bucle de proveedores

        except Exception as exc:
            failed_providers.append(provider)
            _record_call(provider, False)
            logger.warning(f"Proveedor {provider} falló en streaming: {exc}")
            _stats["total_failovers"] += 1
            continue

    else:
        # Todos los proveedores fallaron en streaming
        yield _sse_chunk(req_id, "synergy/auto",
                         "⚠️ Todos los proveedores están experimentando problemas. "
                         "Intenta de nuevo en unos segundos.", "stop")
        yield "data: [DONE]\n\n"
        return

    # Chunk final
    yield _sse_chunk(req_id, used_model, None, "stop")
    yield "data: [DONE]\n\n"


# ── STREAMING: MODO DIRECTO ────────────────────────────────────────────────

async def _stream_direct(
    client: LLMClient,
    system_prompt: str,
    user_message: str,
    provider: str,
    req_id: str,
    model_name: str,
):
    """Streaming directo a un proveedor específico, sin routing."""
    try:
        loop = asyncio.get_event_loop()

        def _stream_gen():
            return list(client.call_stream(system_prompt, user_message, provider))

        chunks = await loop.run_in_executor(None, _stream_gen)

        for token in chunks:
            yield _sse_chunk(req_id, model_name, token)

        yield _sse_chunk(req_id, model_name, None, "stop")
        yield "data: [DONE]\n\n"
        _record_call(provider, True)

    except Exception as exc:
        _record_call(provider, False)
        yield _sse_chunk(req_id, model_name, f"Error: {str(exc)[:200]}", "stop")
        yield "data: [DONE]\n\n"


# ── NON-STREAMING ─────────────────────────────────────────────────────────

async def _non_stream_synergy(
    client: LLMClient,
    system_prompt: str,
    user_message: str,
    validator,
    request: ChatRequest,
    req_id: str,
    t_start: float,
) -> JSONResponse:
    """Non-streaming sinérgico con validación y corrección."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: client.call_with_synergy(system_prompt, user_message, validator=validator),
        )

        if result.synergy_used:
            _stats["total_synergy_corrections"] += 1

        latency = (time.perf_counter() - t_start) * 1000
        provider = result.provider
        model_name = client.providers.get(provider, {}).get("model", provider)
        _record_call(provider, True)

        return JSONResponse({
            "id": f"chatcmpl-{req_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": result.value},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "synergy_meta": {
                "provider": provider,
                "model": model_name,
                "synergy_used": result.synergy_used,
                "synergy_phase": result.phase,
                "validated": result.validated,
                "total_latency_ms": round(latency, 0),
                "attempts": [
                    {"provider": a.provider, "phase": a.phase, "success": a.success,
                     "latency_ms": round(a.latency_ms, 0), "error": a.error}
                    for a in result.attempts
                ],
            },
        })

    except Exception as exc:
        _stats["total_failovers"] += 1
        raise HTTPException(502, f"Sinergia fallida: {str(exc)[:300]}")


async def _non_stream_direct(
    client: LLMClient,
    system_prompt: str,
    user_message: str,
    provider: str,
    model_name: str,
    t_start: float,
) -> JSONResponse:
    """Non-streaming directo a un proveedor."""
    try:
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(
            None, lambda: client.call(system_prompt, user_message)
        )
        _record_call(provider, True)
        latency = (time.perf_counter() - t_start) * 1000

        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    except Exception as exc:
        _record_call(provider, False)
        raise HTTPException(502, f"Proveedor {provider} falló: {str(exc)[:300]}")


# ── ROUTING HELPERS ────────────────────────────────────────────────────────

def _rank_providers(
    client: LLMClient,
    available: List[str],
    mode: str = "auto",
) -> List[tuple]:
    """
    Rankea proveedores usando SmartRouter.
    Devuelve lista de (provider_name, score, reason).
    """
    ranked = []

    for name in available:
        if client._router and name in client._router._targets.get("default", []):
            targets = client._router._targets["default"]
            target = next((t for t in targets if t.id == name), None)
            if target:
                score, reason = client._router._score(target)
                ranked.append((name, score, reason))

    if not ranked:
        # Fallback: usar orden de disponibilidad
        ranked = [(name, 1.0, "fallback") for name in available]

    ranked.sort(key=lambda x: x[1], reverse=True)

    # Modo "fast": priorizar tier 1 (groq, cerebras)
    if mode == "fast":
        fast_names = {"groq", "cerebras"}
        fast = [(n, s, r) for n, s, r in ranked if n in fast_names]
        rest = [(n, s, r) for n, s, r in ranked if n not in fast_names]
        ranked = fast + rest

    # Modo "quality": priorizar tier 2+ (gemini_flash, modal)
    elif mode == "quality":
        quality_names = {"gemini_flash", "modal", "nvidia"}
        quality = [(n, s, r) for n, s, r in ranked if n in quality_names]
        rest = [(n, s, r) for n, s, r in ranked if n not in quality_names]
        ranked = quality + rest

    return ranked


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Synergy Router Proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logger.info(f"🚀 Synergy Router Proxy iniciando en {args.host}:{args.port}")
    logger.info("   Proveedores: se cargan desde config/llm.json + .credentials.json")
    logger.info("   Web Search: DuckDuckGo (gratuito, sin API key)")
    logger.info("   Registro en pi: ~/.pi/agent/models.json → provider 'synergy'")

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
