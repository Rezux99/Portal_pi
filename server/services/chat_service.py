"""
chat_service.py — Lógica de negocio para El Analista (chat RAG).
Conecta con LLMClient real para generar respuestas inteligentes.
Soporta modo Supabase (DB queries + per-user chat history).
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import List, Optional, Dict, Any

from scripts.paths import RAW_DIR, SYNTHESIZED_DIR, REPORTS_DIR
from scripts.supabase_client import use_supabase
from server.schemas import ChatRequest, ChatResponse

SYSTEM_PROMPT = """Eres el Analista de Portal Pi, un sistema de inteligencia periodística. Tu función es:

1. Analizar noticias: Resume, contextualiza y extrae implicaciones de artículos informativos.
2. Generar informes: Crea análisis estructurados con hallazgos clave, contexto y recomendaciones.
3. Responder preguntas: Con base en el contexto proporcionado, responde de forma precisa y citando fuentes cuando sea posible.
4. Detectar patrones: Identifica tendencias, conexiones entre eventos y señales emergentes.

Reglas:
- Responde SIEMPRE en español.
- Sé conciso pero completo.
- Cita las fuentes del contexto cuando sea relevante.
- Si no tienes suficiente contexto, indícalo claramente.
- Usa formato markdown para estructurar tus respuestas.
- No inventes información que no esté en el contexto proporcionado.
"""


def _gather_context(context_files: Optional[List[str]] = None, query: str = "", user: Optional[Dict[str, Any]] = None) -> str:
    """Recopila contexto de archivos especificados o busca por relevancia."""
    context_parts = []

    # ── Modo Supabase: buscar en DB ──
    if use_supabase() and not context_files:
        try:
            from server.deps import get_db
            db = get_db()
            q_lower = query.lower()
            keywords = q_lower.split()[:5]

            # Buscar en raw_news por título
            result = db._client.table("raw_news").select("filename,title,content,source").order("ingested_at", desc=True).limit(8).execute()
            for row in result.data:
                content = row.get("content", "")
                title = row.get("title", "")
                score = sum(1 for kw in keywords if kw in (title + " " + content).lower())
                if score > 0 or not keywords:
                    context_parts.append(content[:2500])
                    if len(context_parts) >= 8:
                        break

            # Buscar en syntheses
            if len(context_parts) < 8:
                synth = db._client.table("syntheses").select("executive_summary,priority").order("created_at", desc=True).limit(3).execute()
                for row in synth.data:
                    ctx = f"Resumen: {row.get('executive_summary', '')} (Prioridad: {row.get('priority', '')})"
                    context_parts.append(ctx)
        except Exception:
            pass

        return "\n\n---\n\n".join(context_parts)[:10000]

    # ── Modo filesystem ──
    if context_files:
        for fname in context_files:
            for base_dir in [RAW_DIR, SYNTHESIZED_DIR, REPORTS_DIR]:
                fp = Path(base_dir) / fname
                if fp.exists():
                    context_parts.append(fp.read_text(encoding="utf-8", errors="replace")[:3000])
                    break

    if not context_parts and query:
        q_lower = query.lower()
        keywords = q_lower.split()[:5]
        raw_path = Path(RAW_DIR)
        if raw_path.exists():
            count = 0
            for f in sorted(raw_path.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                score = sum(1 for kw in keywords if kw in content.lower())
                if score > 0:
                    context_parts.append(content[:2500])
                    count += 1
                    if count >= 8:
                        break

        synth_path = Path(SYNTHESIZED_DIR)
        if synth_path.exists() and len(context_parts) < 8:
            for f in sorted(synth_path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    if q_lower in content.lower():
                        context_parts.append(content[:2000])
                        if len(context_parts) >= 8:
                            break
                except Exception:
                    continue

    return "\n\n---\n\n".join(context_parts)[:10000]


def _save_chat_message(user: Optional[Dict[str, Any]], role: str, content: str,
                       context_files: Optional[List[str]] = None) -> None:
    """Guarda mensaje de chat en Supabase si está configurado."""
    if use_supabase() and user and user.get("id") != "local-user":
        try:
            from server.deps import get_db
            db = get_db()
            db.insert_chat_message(user["id"], role, content, context_files)
        except Exception:
            pass


def _get_llm_client():
    """Obtiene instancia de LLMClient (lazy init)."""
    from scripts.llm_client import LLMClient, LLMClientError
    try:
        return LLMClient()
    except LLMClientError:
        return None


def chat(request: ChatRequest, user: Optional[Dict[str, Any]] = None) -> ChatResponse:
    """Procesa un mensaje de chat y devuelve respuesta usando LLM real."""
    context = _gather_context(request.context_files, request.message, user)
    llm = _get_llm_client()

    # Guardar mensaje del usuario
    _save_chat_message(user, "user", request.message, request.context_files)

    if llm is not None:
        try:
            if context:
                user_prompt = (
                    f"Contexto disponible:\n```\n{context[:6000]}\n```\n\n"
                    f"Pregunta del usuario: {request.message}\n\n"
                    f"Responde basándote en el contexto proporcionado. Cita las fuentes relevantes."
                )
            else:
                user_prompt = (
                    f"Pregunta del usuario: {request.message}\n\n"
                    f"No hay contexto específico disponible. Responde con tu conocimiento general "
                    f"pero indica que no tienes fuentes locales para esta consulta."
                )

            reply = llm.call(SYSTEM_PROMPT, user_prompt)

            # Guardar respuesta
            _save_chat_message(user, "assistant", reply)

            return ChatResponse(reply=reply, sources=request.context_files or [])

        except Exception as e:
            reply = f"⚠️ **El LLM no está disponible** ({type(e).__name__}: {str(e)[:100]})\n\n---\n\n"
            if context:
                reply += (
                    f"He encontrado contexto relevante para tu consulta:\n\n"
                    f"**Tu pregunta:** *{request.message}*\n\n"
                    f"**Contexto encontrado** ({len(context)} caracteres):\n"
                    f"```\n{context[:1500]}\n```\n\n"
                    f"*Configura las API keys en Operaciones para habilitar El Analista.*"
                )
            else:
                reply += f"No encontré contexto específico para: *{request.message}*\n\n*Configura las API keys en Operaciones para habilitar El Analista.*"
            return ChatResponse(reply=reply, sources=request.context_files or [])

    # Modo demo (sin LLM configurado)
    if context:
        reply = (
            f"**Modo demo** — He encontrado contexto relevante para tu consulta.\n\n"
            f"**Tu pregunta:** *{request.message}*\n\n"
            f"**Contexto encontrado** ({len(context)} caracteres):\n"
            f"```\n{context[:1500]}\n```\n\n"
            f"*Configura las API keys en ⚙️ Operaciones para habilitar respuestas inteligentes.*"
        )
    else:
        reply = f"**Modo demo** — No encontré contexto específico para: *{request.message}*\n\n*Configura las API keys en ⚙️ Operaciones para habilitar El Analista con LLM.*"

    return ChatResponse(reply=reply, sources=request.context_files or [])
