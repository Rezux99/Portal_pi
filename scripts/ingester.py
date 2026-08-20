"""
ingester.py
Módulo de ingesta automática de feeds RSS/Atom para Portal Pi.
Descarga artículos, los normaliza y los persiste en raw_news/ + DB.
Soporta Supabase (DB + Storage) y filesystem (local).
"""

import json
import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

import feedparser
import requests

from scripts.database import PortalDatabase, DatabaseError
from scripts.supabase_client import use_supabase
from scripts.supabase_database import SupabaseDatabase
from scripts.supabase_storage import get_storage, SupabaseStorage

from scripts.paths import BASE_DIR, RAW_DIR, DB_PATH, LOGS_DIR, FEEDS_CONFIG_PATH, INGESTER_LOG, log_to_file, load_json_config, save_json_config


@dataclass
class IngestResult:
    """Resultado de una sesión de ingesta."""
    feed_name: str
    articles_found: int = 0
    articles_new: int = 0
    articles_skipped: int = 0
    errors: List[str] = field(default_factory=list)
    elapsed_sec: float = 0.0


class FeedIngester:
    """
    Ingestor de feeds RSS/Atom.
    - Lee configuración de config/feeds.json
    - Descarga y parsea feeds
    - Normaliza artículos a formato texto
    - Deduplica por checksum de contenido
    - Guarda en data/raw_news/ y registra en DB
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> None:
        self.config_path = Path(config_path) if config_path else FEEDS_CONFIG_PATH
        self._use_sb = use_supabase()
        self.db = self._get_db(db_path)
        self._storage = get_storage() if self._use_sb else None
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "PortalPi/1.0 (News Aggregator)"
        })
        self._load_config()

    def _get_db(self, db_path: Optional[str] = None):
        if self._use_sb:
            return SupabaseDatabase()
        return PortalDatabase(db_path or str(DB_PATH))

    def _load_config(self) -> None:
        """Carga la configuración de feeds. En Supabase, lee de feed_configs table."""
        if self._use_sb:
            try:
                feeds = self.db.list_feed_configs()
                # Auto-seed: si la tabla está vacía, importar desde config/feeds.json
                if not feeds:
                    self._seed_feeds_from_json()
                    feeds = self.db.list_feed_configs()
                settings = {}
                state = self.db.get_state("ingester_settings")
                if state:
                    settings = state
                self.config = {"feeds": feeds, "settings": settings}
            except Exception:
                self.config = {"feeds": [], "settings": {}}
            return

        self.config = load_json_config(self.config_path, default={"feeds": [], "settings": {}})
        if not self.config:
            self.config = {"feeds": [], "settings": {}}
            self._save_config()

    def _seed_feeds_from_json(self) -> None:
        """Si la tabla feed_configs de Supabase está vacía, la puebla desde config/feeds.json."""
        try:
            local_config = load_json_config(self.config_path, default={"feeds": []})
            local_feeds = local_config.get("feeds", [])
            if not local_feeds:
                return
            self._log(f"Auto-seeding {len(local_feeds)} feeds from config/feeds.json → Supabase feed_configs")
            for f in local_feeds:
                try:
                    self.db.upsert_feed_config(
                        name=f.get("name", ""),
                        url=f.get("url", ""),
                        category=f.get("category", "Otro"),
                        enabled=f.get("enabled", True),
                        poll_interval_min=f.get("poll_interval_min", 30),
                    )
                except Exception:
                    pass
        except Exception:
            pass

    def _save_config(self) -> None:
        """Persiste la configuración de feeds."""
        save_json_config(self.config_path, self.config)

    # ─── LOGGING INTERNO ──────────────────────────────────────────────────

    def _log(self, message: str, level: str = "INFO") -> None:
        log_to_file(INGESTER_LOG, message, level)

    # ─── NORMALIZACIÓN ───────────────────────────────────────────────────

    def _sanitize_filename(self, title: str, max_len: int = 60) -> str:
        """Convierte un título en un nombre de archivo seguro."""
        # Quitar acentos básicos
        replacements = {
            "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
            "ñ": "n", "ü": "u", "Á": "A", "É": "E", "Í": "I",
            "Ó": "O", "Ú": "U", "Ñ": "N", "Ü": "U",
        }
        for old, new in replacements.items():
            title = title.replace(old, new)
        # Solo alfanuméricos, guiones y underscores
        title = re.sub(r"[^\w\s\-]", "", title)
        title = re.sub(r"[\s]+", "_", title.strip())
        # Truncar
        if len(title) > max_len:
            title = title[:max_len].rstrip("_")
        return title.lower()

    def _extract_text(self, entry: Dict[str, Any]) -> str:
        """Extrae el texto más limpio posible de una entrada de feed."""
        # Intentar summary_detail primero (suele ser más limpio)
        if "summary_detail" in entry and "value" in entry["summary_detail"]:
            text = entry["summary_detail"]["value"]
        elif "summary" in entry:
            text = entry["summary"]
        elif "content" in entry and isinstance(entry["content"], list) and entry["content"]:
            text = entry["content"][0].get("value", "")
        elif "description" in entry:
            text = entry["description"]
        else:
            text = ""

        # Limpiar HTML básico
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _compute_checksum(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # ─── RESOLUCIÓN DE ENLACES ────────────────────────────────────────────

    def _resolve_link(self, link: str, timeout: int = 8) -> Tuple[str, str]:
        """
        Intenta resolver un enlace intermedio (ej: Google News) al enlace directo.
        Devuelve (resolved_url, link_type).
        link_type: 'direct' | 'indirect' | 'none'
        """
        if not link or not link.startswith('http'):
            return '', 'none'

        # Si no es Google News, es probablemente directo
        if 'news.google.com' not in link:
            return link, 'direct'

        # Intentar resolver enlace de Google News
        try:
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            })
            # Usar GET con stream para no descargar todo
            r = session.get(link, allow_redirects=True, timeout=timeout, stream=True)
            r.close()  # No necesitamos el body

            final_url = r.url
            # Si la URL final no es de Google, tenemos el enlace real
            if 'google.com' not in final_url and 'consent.google.com' not in final_url:
                return final_url, 'direct'
        except (requests.RequestException, Exception):
            pass

        # No se pudo resolver — es indirecto
        return link, 'indirect'

    # ─── DESCARGA DE FEED ────────────────────────────────────────────────

    def fetch_feed(self, url: str, timeout: int = 30) -> feedparser.FeedParserDict:
        """Descarga y parsea un feed RSS/Atom."""
        try:
            response = self._session.get(url, timeout=timeout)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if feed.bozo and not feed.entries:
                raise ValueError(f"Feed malformado: {feed.bozo_exception}")
            return feed
        except requests.RequestException as exc:
            raise ConnectionError(f"Error descargando feed {url}: {exc}") from exc

    # ─── INGESTA DE UN ARTÍCULO ──────────────────────────────────────────

    def _ingest_article(
        self,
        entry: Dict[str, Any],
        feed_config: Dict[str, Any],
        result: IngestResult,
    ) -> bool:
        """
        Procesa un solo artículo: normaliza, deduplica, guarda.
        Devuelve True si fue ingestado (nuevo), False si se saltó.
        """
        title = entry.get("title", "Sin título").strip()
        link = entry.get("link", "")
        published = entry.get("published", "")

        # Extraer texto
        body = self._extract_text(entry)
        if not body or len(body) < 50:
            result.articles_skipped += 1
            return False

        # Resolver enlace (Google News → fuente original)
        resolved_link, link_type = self._resolve_link(link)

        # Construir contenido normalizado
        # NOTA: FECHA_INGESTA se excluye del checksum para evitar
        # duplicados del mismo artículo con distinta fecha de ingesta.
        content_parts = [
            f"FUENTE: {feed_config['name']}",
            f"CATEGORÍA: {feed_config.get('category', 'Otro')}",
            f"TÍTULO: {title}",
            f"ENLACE: {link}",
        ]
        # Añadir enlace resuelto si es diferente al original
        if resolved_link and resolved_link != link:
            content_parts.append(f"ENLACE_RESUELTO: {resolved_link}")
        content_parts.append(f"TIPO_ENLACE: {link_type}")
        content_parts.extend([
            f"FECHA_PUBLICACIÓN: {published}",
            "",
            body,
        ])
        content_for_checksum = "\n".join(content_parts)
        checksum = self._compute_checksum(content_for_checksum)

        # Añadir FECHA_INGESTA solo al archivo final (no al checksum)
        content_parts.insert(len(content_parts) - 2, f"FECHA_INGESTA: {datetime.now(timezone.utc).isoformat()}")
        content = "\n".join(content_parts)

        # Deduplicar por checksum en DB
        if self.db.raw_news_exists(checksum):
            result.articles_skipped += 1
            return False

        # Deduplicar por título si está configurado
        settings = self.config.get("settings", {})
        if settings.get("dedup_by_title", True):
            safe_name = self._sanitize_filename(title)
            candidate_path = RAW_DIR / f"{safe_name}.txt"
            if candidate_path.exists():
                # Verificar si es exactamente el mismo contenido
                existing = candidate_path.read_text(encoding="utf-8")
                if self._compute_checksum(existing) == checksum:
                    result.articles_skipped += 1
                    return False
                # Mismo nombre pero contenido distinto — añadir timestamp
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                safe_name = f"{safe_name}_{ts}"

        # Guardar archivo raw
        filename = f"{self._sanitize_filename(title)}.txt"
        out_path = RAW_DIR / filename

        # Evitar colisiones
        counter = 1
        original_stem = out_path.stem
        while out_path.exists():
            out_path = RAW_DIR / f"{original_stem}_{counter}.txt"
            counter += 1

        try:
            out_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            result.errors.append(f"Error escribiendo {filename}: {exc}")
            return False

        # Registrar en DB
        try:
            if self._use_sb:
                # Modo Supabase: insertar con metadatos completos + Storage
                self.db.insert_raw_news_full(
                    filename=filename, content=content, checksum=checksum,
                    source=feed_config['name'],
                    category=feed_config.get('category', 'Otro'),
                    title=title, link=link, link_type=link_type,
                    published=published,
                )
                # Subir a Supabase Storage
                if self._storage:
                    try:
                        self._storage.save_raw_news(filename, content)
                    except Exception as exc:
                        result.errors.append(f"Error Storage para {filename}: {exc}")
            else:
                self.db.insert_raw_news(filename, content, checksum)
        except DatabaseError as exc:
            result.errors.append(f"Error DB para {filename}: {exc}")

        result.articles_new += 1
        self._log(f"Artículo ingestado: {title[:60]}...")
        return True

    # ─── INGESTA COMPLETA ────────────────────────────────────────────────

    def ingest_all(self, only_enabled: bool = True) -> List[IngestResult]:
        """Ejecuta ingesta de todos los feeds configurados."""
        results = []
        settings = self.config.get("settings", {})
        max_per_feed = settings.get("max_articles_per_feed", 10)

        for feed_cfg in self.config.get("feeds", []):
            if only_enabled and not feed_cfg.get("enabled", True):
                continue

            result = IngestResult(feed_name=feed_cfg["name"])
            t0 = time.time()

            try:
                self._log(f"Ingestando feed: {feed_cfg['name']} ({feed_cfg['url']})")
                feed = self.fetch_feed(feed_cfg["url"])
                result.articles_found = len(feed.entries)

                for entry in feed.entries[:max_per_feed]:
                    try:
                        self._ingest_article(entry, feed_cfg, result)
                    except Exception as exc:
                        result.errors.append(f"Error en artículo: {exc}")

            except (ConnectionError, ValueError) as exc:
                result.errors.append(str(exc))
                self._log(f"Error en feed {feed_cfg['name']}: {exc}", "ERROR")

            result.elapsed_sec = round(time.time() - t0, 2)
            results.append(result)
            self._log(
                f"Feed {feed_cfg['name']}: {result.articles_new} nuevos, "
                f"{result.articles_skipped} duplicados, "
                f"{len(result.errors)} errores ({result.elapsed_sec}s)"
            )

        return results

    def ingest_feed(self, feed_name: str) -> Optional[IngestResult]:
        """Ingusta un feed específico por nombre."""
        for feed_cfg in self.config.get("feeds", []):
            if feed_cfg["name"] == feed_name:
                result = IngestResult(feed_name=feed_name)
                t0 = time.time()
                settings = self.config.get("settings", {})
                max_per_feed = settings.get("max_articles_per_feed", 10)

                try:
                    feed = self.fetch_feed(feed_cfg["url"])
                    result.articles_found = len(feed.entries)
                    for entry in feed.entries[:max_per_feed]:
                        try:
                            self._ingest_article(entry, feed_cfg, result)
                        except Exception as exc:
                            result.errors.append(str(exc))
                except (ConnectionError, ValueError) as exc:
                    result.errors.append(str(exc))

                result.elapsed_sec = round(time.time() - t0, 2)
                return result

        return None

    # ─── GESTIÓN DE FEEDS ────────────────────────────────────────────────

    def list_feeds(self) -> List[Dict[str, Any]]:
        """Devuelve la lista de feeds configurados."""
        return self.config.get("feeds", [])

    def add_feed(self, name: str, url: str, category: str = "Otro", poll_interval_min: int = 30) -> Dict[str, Any]:
        """Añade un nuevo feed a la configuración."""
        new_feed = {
            "name": name,
            "url": url,
            "category": category,
            "enabled": True,
            "poll_interval_min": poll_interval_min,
        }
        self.config.setdefault("feeds", []).append(new_feed)
        self._save_config()
        if self._use_sb:
            try:
                self.db.upsert_feed_config(name, url, category, True, poll_interval_min)
            except Exception:
                pass
        self._log(f"Feed añadido: {name}")
        return new_feed

    def remove_feed(self, name: str) -> bool:
        """Elimina un feed por nombre."""
        feeds = self.config.get("feeds", [])
        original_len = len(feeds)
        self.config["feeds"] = [f for f in feeds if f["name"] != name]
        if len(self.config["feeds"]) < original_len:
            self._save_config()
            if self._use_sb:
                try:
                    self.db.delete_feed_config(name)
                except Exception:
                    pass
            self._log(f"Feed eliminado: {name}")
            return True
        return False

    def toggle_feed(self, name: str) -> Optional[bool]:
        """Activa/desactiva un feed. Devuelve el nuevo estado o None si no existe."""
        for feed in self.config.get("feeds", []):
            if feed["name"] == name:
                feed["enabled"] = not feed.get("enabled", True)
                self._save_config()
                if self._use_sb:
                    try:
                        self.db.upsert_feed_config(
                            feed["name"], feed["url"], feed.get("category", "Otro"),
                            feed["enabled"], feed.get("poll_interval_min", 30),
                        )
                    except Exception:
                        pass
                self._log(f"Feed {name}: {'habilitado' if feed['enabled'] else 'deshabilitado'}")
                return feed["enabled"]
        return None

    # ─── ESTADÍSTICAS ────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """Devuelve estadísticas del ingester."""
        feeds = self.config.get("feeds", [])
        raw_files = list(RAW_DIR.glob("*.txt")) + list(RAW_DIR.glob("*.md"))
        db_stats = self.db.stats()

        return {
            "total_feeds": len(feeds),
            "enabled_feeds": sum(1 for f in feeds if f.get("enabled", True)),
            "raw_articles_on_disk": len(raw_files),
            "raw_articles_in_db": db_stats.get("raw_news", 0),
            "categories": list(set(f.get("category", "Otro") for f in feeds)),
        }
