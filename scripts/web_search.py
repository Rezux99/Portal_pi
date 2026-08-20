"""
web_search.py — Portal Pi
==========================
Búsqueda web integrada para el chat de Portal Pi.

Usa DuckDuckGo Search (sin API key, sin coste) para buscar noticias
en tiempo real e inyectarlas como contexto en el chat.

Flujo:
  1. El usuario pregunta algo que requiere datos actuales.
  2. El sistema detecta si la pregunta necesita búsqueda web.
  3. Busca en DuckDuckGo News + Web, obtiene resultados relevantes.
  4. Inyecta los resultados como CONTEXTO VERIFICABLE en el system prompt.
  5. El LLM responde basándose en hechos, no inventando.

Dependencias: requests, beautifulsoup4 (o httpx como fallback).
No requiere API keys — DuckDuckGo es gratis.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import requests

logger = logging.getLogger("web_search")


# ─── DATOS ────────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """Resultado individual de búsqueda."""
    title: str
    url: str
    snippet: str
    source: str = ""
    date: str = ""
    engine: str = "duckduckgo"


@dataclass
class SearchResponse:
    """Respuesta completa de una búsqueda."""
    query: str
    results: List[SearchResult] = field(default_factory=list)
    total_found: int = 0
    elapsed_sec: float = 0.0
    error: Optional[str] = None

    def to_context_text(self, max_results: int = 8, max_snippet: int = 300) -> str:
        """Convierte los resultados a texto para inyectar como contexto en el LLM."""
        if not self.results:
            return "No se encontraron resultados en la web para esta consulta."

        lines = []
        for i, r in enumerate(self.results[:max_results], 1):
            meta = []
            if r.source:
                meta.append(f"Fuente: {r.source}")
            if r.date:
                meta.append(f"Fecha: {r.date}")
            meta_str = f" ({', '.join(meta)})" if meta else ""

            snippet = r.snippet[:max_snippet]
            lines.append(
                f"{i}. {r.title}{meta_str}\n"
                f"   {snippet}\n"
                f"   Enlace: {r.url}"
            )
        return "\n\n".join(lines)


# ─── DETECCIÓN DE NECESIDAD DE BÚSQUEDA ────────────────────────────────────

# Patrones que indican que el usuario pregunta por algo que necesita datos actuales
_SEARCH_PATTERNS = [
    # Temporal — piden información actual
    r"\b(últim[oa]|reciente|actual|hoy|ayer|esta semana|este mes|ahora)\b",
    r"\b(latest|recent|current|today|yesterday|this week|this month|now)\b",
    # Eventos — piden por algo que pasa
    r"\b(qué pasó|que paso|qué ha pasado|qué está pasando|qué ocurre|sucede)\b",
    r"\b(what happened|what's happening|what is going on)\b",
    # Búsqueda explícita
    r"\b(busca|buscar|encuentra|búsqueda|search|find|look up)\b",
    # Noticias
    r"\b(noticias?|news|novedades?|nuevo|nueva|nuevos|nuevas)\b",
    # Personas/organizaciones actuales
    r"\b(quién es|who is|quién ha|who has|quién dijo)\b",
    # Geopolítica / economía
    r"\b(mercados?|bolsa|índice|inflación|PIB|interés|tipo de cambio)\b",
    r"\b(market|stock|index|inflation|GDP|interest rate|exchange rate)\b",
    r"\b(conflicto|guerra|acuerdo|tratado|sanción|negociación)\b",
    r"\b(conflict|war|agreement|treaty|sanction|negotiation)\b",
    # Comparación con el presente
    r"\b(en \d{4}|this year|el año pasado|last year)\b",
]

_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _SEARCH_PATTERNS]

# Patrones que indican que NO necesita búsqueda (opinión, ayuda con el sistema)
_NO_SEARCH_PATTERNS = [
    re.compile(r"\b(cómo uso|how do i use|ayuda con|help with|configura|setup)\b", re.IGNORECASE),
    re.compile(r"\b(qué es portal pi|what is portal pi|qué puedes hacer)\b", re.IGNORECASE),
]


def needs_web_search(user_message: str) -> bool:
    """
    Determina si la pregunta del usuario necesita una búsqueda web.

    Usa heurísticas simples: si contiene indicadores temporales,
    de actualidad o búsqueda explícita, y no es una pregunta sobre
    el propio sistema, devuelve True.
    """
    if not user_message or len(user_message.strip()) < 5:
        return False

    # Si es una pregunta sobre el propio sistema, no buscar
    for p in _NO_SEARCH_PATTERNS:
        if p.search(user_message):
            return False

    # Si coincide con algún patrón de búsqueda
    for p in _COMPILED_PATTERNS:
        if p.search(user_message):
            return True

    return False


def extract_search_query(user_message: str, entities: List[str] = None) -> str:
    """
    Extrae una query de búsqueda limpia del mensaje del usuario.

    Si el mensaje es "busca noticias sobre la economía de España",
    la query será "economía España noticias recientes".
    """
    query = user_message.strip()

    # Quitar prefijos de comando comunes
    for prefix in ["busca ", "buscar ", "encuentra ", "search ", "find ", "look up "]:
        if query.lower().startswith(prefix):
            query = query[len(prefix):].strip()

    # Añadir entidades si están disponibles (contexto del sistema)
    if entities:
        entity_str = " ".join(entities[:3])
        if entity_str and entity_str.lower() not in query.lower():
            query = f"{query} {entity_str}"

    # Truncar si es muy largo
    if len(query) > 200:
        query = query[:200]

    return query


# ─── DUCKDUCKGO SEARCH (Sin API key) ─────────────────────────────────────

class DuckDuckGoSearcher:
    """
    Búsqueda en DuckDuckGo sin API key.

    Usa dos endpoints:
    1. DDG News API (https://duckduckgo.com/ddg_news) — noticias recientes.
    2. DDG HTML (https://html.duckduckgo.com) — resultados web generales.

    Ambos son gratuitos y no requieren autenticación.
    """

    def __init__(self, timeout: int = 10, max_results: int = 10):
        self.timeout = timeout
        self.max_results = max_results
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "es,en;q=0.9",
        })
        self._last_request_time = 0.0
        self._min_interval = 1.0  # Segundos entre peticiones (rate limit)

    def _rate_limit(self) -> None:
        """Espera si es necesario para no hacer demasiadas peticiones."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def search_news(self, query: str, max_results: int = None) -> List[SearchResult]:
        """Busca noticias recientes en DuckDuckGo News."""
        self._rate_limit()
        max_results = max_results or self.max_results

        try:
            # DuckDuckGo News API endpoint
            url = "https://duckduckgo.com/ddg_news"
            params = {
                "q": query,
                "l": "wt-wt",  # Sin región específica (worldwide)
                "o": "json",
                "no_redirect": 1,
            }

            r = self._session.get(url, params=params, timeout=self.timeout)
            r.raise_for_status()

            data = r.json()
            results = []

            # DDG News devuelve resultados en diferentes formatos
            # Formato 1: lista directa
            news_items = []
            if isinstance(data, dict):
                # Buscar la clave que contenga los resultados
                for key in ["results", "news", "RelatedTopics"]:
                    if key in data and isinstance(data[key], list):
                        news_items = data[key]
                        break
            elif isinstance(data, list):
                news_items = data

            for item in news_items[:max_results]:
                if isinstance(item, dict):
                    title = item.get("title", "")
                    url_val = item.get("url", item.get("link", ""))
                    snippet = item.get("body", item.get("snippet", item.get("text", "")))
                    source = item.get("source", item.get("provider", ""))
                    date = item.get("date", item.get("age", ""))

                    if not url_val and "FirstURL" in item:
                        url_val = item["FirstURL"]
                    if not snippet and "Text" in item:
                        snippet = item["Text"]

                    if title and (snippet or url_val):
                        results.append(SearchResult(
                            title=title,
                            url=url_val,
                            snippet=snippet or "",
                            source=source,
                            date=date,
                            engine="duckduckgo_news",
                        ))

            return results

        except Exception as e:
            logger.warning(f"DDG News search failed for '{query}': {e}")
            return []

    def search_web(self, query: str, max_results: int = None) -> List[SearchResult]:
        """Busca en DuckDuckGo Web (HTML scraping)."""
        self._rate_limit()
        max_results = max_results or self.max_results

        try:
            url = "https://html.duckduckgo.com/html/"
            data = {"q": query, "b": ""}

            r = self._session.post(url, data=data, timeout=self.timeout)
            r.raise_for_status()

            results = []
            html = r.text

            # Parsear resultados del HTML de DDG
            # Cada resultado está en un div con class="result"
            # Título en <a class="result__a">
            # Snippet en <a class="result__snippet">
            # URL en el href del título

            result_blocks = re.findall(
                r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
                r'.*?'
                r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
                html,
                re.DOTALL,
            )

            for link, title_html, snippet_html in result_blocks[:max_results]:
                title = re.sub(r"<[^>]+>", "", title_html).strip()
                snippet = re.sub(r"<[^>]+>", "", snippet_html).strip()

                # Limpiar URL (DDG usa redirect)
                if link.startswith("//"):
                    link = "https:" + link

                # Extraer dominio como fuente
                source = ""
                try:
                    from urllib.parse import urlparse
                    source = urlparse(link).netloc.replace("www.", "")
                except Exception:
                    pass

                if title:
                    results.append(SearchResult(
                        title=title,
                        url=link,
                        snippet=snippet,
                        source=source,
                        engine="duckduckgo_web",
                    ))

            return results

        except Exception as e:
            logger.warning(f"DDG Web search failed for '{query}': {e}")
            return []

    def search(self, query: str, max_results: int = None) -> SearchResponse:
        """
        Búsqueda completa: noticias + web, deduplicada.

        Intenta primero noticias (más relevantes para actualidad),
        luego completa con resultados web generales.
        """
        t0 = time.time()
        max_results = max_results or self.max_results

        all_results: List[SearchResult] = []

        # 1. Noticias (prioridad para actualidad)
        try:
            news = self.search_news(query, max_results=max_results)
            all_results.extend(news)
        except Exception as e:
            logger.warning(f"News search error: {e}")

        # 2. Web general (completar si faltan resultados)
        if len(all_results) < max_results:
            try:
                web = self.search_web(query, max_results=max_results - len(all_results))
                all_results.extend(web)
            except Exception as e:
                logger.warning(f"Web search error: {e}")

        # 3. Deduplicar por URL
        seen_urls = set()
        unique = []
        for r in all_results:
            normalized_url = r.url.split("?")[0].rstrip("/")  # Sin query params ni trailing slash
            if normalized_url not in seen_urls and r.url:
                seen_urls.add(normalized_url)
                unique.append(r)

        elapsed = time.time() - t0
        logger.info(f"Web search '{query}': {len(unique)} results in {elapsed:.1f}s")

        return SearchResponse(
            query=query,
            results=unique,
            total_found=len(unique),
            elapsed_sec=round(elapsed, 2),
        )


# ─── INSTANCIA GLOBAL ──────────────────────────────────────────────────────

_searcher: Optional[DuckDuckGoSearcher] = None


def get_searcher() -> DuckDuckGoSearcher:
    """Devuelve la instancia singleton del searcher."""
    global _searcher
    if _searcher is None:
        _searcher = DuckDuckGoSearcher()
    return _searcher


def web_search(query: str, max_results: int = 10) -> SearchResponse:
    """Función de conveniencia para buscar desde cualquier módulo."""
    return get_searcher().search(query, max_results=max_results)


# ─── BÚSQUEDA DESDE CONTEXTO DEL SISTEMA ───────────────────────────────────

def search_from_pipeline_context(
    user_message: str,
    entity_names: List[str] = None,
    categories: List[str] = None,
) -> SearchResponse:
    """
    Construye una query de búsqueda inteligente usando el contexto del pipeline.

    Si el usuario pregunta "¿qué está pasando con la economía?" y el pipeline
    ha detectado categorías como "Economía" y entidades como "Banco Central Europeo",
    la query será "economía Banco Central Europeo noticias recientes".
    """
    query = extract_search_query(user_message, entities=entity_names)

    # Añadir contexto de categorías si están disponibles
    if categories:
        cat_str = " ".join(categories[:2])
        if cat_str.lower() not in query.lower():
            query = f"{query} {cat_str}"

    # Añadir "noticias recientes" si parece que pregunta por actualidad
    if needs_web_search(user_message):
        if "noticias" not in query.lower() and "news" not in query.lower():
            query = f"{query} noticias recientes"

    return web_search(query)


__all__ = [
    "DuckDuckGoSearcher",
    "SearchResult",
    "SearchResponse",
    "web_search",
    "search_from_pipeline_context",
    "needs_web_search",
    "extract_search_query",
]
