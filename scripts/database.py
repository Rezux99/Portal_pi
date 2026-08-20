"""
database.py
Módulo de persistencia SQLite para Portal Pi.
Almacena toda la información destilada por el pipeline en tablas consultables.
"""

import json
import sqlite3
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime


class DatabaseError(Exception):
    """Error en operaciones de base de datos."""
    pass


class PortalDatabase:
    """
    Interfaz de base de datos para Portal Pi.
    Todas las operaciones son transaccionales.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS raw_news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL UNIQUE,
        content TEXT NOT NULL,
        checksum TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        processed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        confidence REAL,
        mentions TEXT,
        source_file TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT NOT NULL,
        predicate TEXT NOT NULL,
        object TEXT NOT NULL,
        source_file TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS syntheses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        executive_summary TEXT NOT NULL,
        priority TEXT,
        trends TEXT,
        source_files TEXT,
        output_filename TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS classifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        primary_category TEXT NOT NULL,
        secondary_tags TEXT,
        justification TEXT,
        source_file TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS action_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT,
        description TEXT NOT NULL,
        owner TEXT,
        deadline TEXT,
        priority TEXT,
        source_synthesis TEXT,
        created_at TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str) -> None:
        self.db_path: Path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(self.SCHEMA)
            # ── Migración: añadir columna processed_at si no existe ──
            try:
                conn.execute("ALTER TABLE raw_news ADD COLUMN processed_at TEXT")
            except Exception:
                pass  # Ya existe

    def _now(self) -> str:
        return datetime.utcnow().isoformat() + "Z"

    # ─── INSERCIÓN RAW NEWS ──────────────────────────────────────────────

    def insert_raw_news(self, filename: str, content: str, checksum: str) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO raw_news (filename, content, checksum, ingested_at) VALUES (?, ?, ?, ?)",
                (filename, content, checksum, self._now())
            )
            conn.commit()
            return cursor.lastrowid

    def raw_news_exists(self, checksum: str) -> bool:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM raw_news WHERE checksum = ?", (checksum,)
            ).fetchone()
            return row is not None

    def mark_raw_news_processed(self, filenames: List[str]) -> int:
        """Marca archivos raw como procesados. Devuelve el número actualizados."""
        count = 0
        with self._get_conn() as conn:
            for fn in filenames:
                cursor = conn.execute(
                    "UPDATE raw_news SET processed_at = ? WHERE filename = ? AND processed_at IS NULL",
                    (self._now(), fn)
                )
                count += cursor.rowcount
            conn.commit()
        return count

    def get_unprocessed_filenames(self) -> List[str]:
        """Devuelve filenames de raw_news que aún no han sido procesados."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT filename FROM raw_news WHERE processed_at IS NULL ORDER BY ingested_at DESC"
            ).fetchall()
            return [r[0] for r in rows]

    # ─── INSERCIÓN ENTIDADES ─────────────────────────────────────────────

    def insert_entities(self, entities: List[Dict[str, Any]], source_file: str) -> int:
        """Inserta una lista de entidades. Devuelve el número de filas insertadas."""
        count = 0
        with self._get_conn() as conn:
            for ent in entities:
                mentions = json.dumps(ent.get("mentions", []), ensure_ascii=False)
                conn.execute(
                    "INSERT INTO entities (name, type, confidence, mentions, source_file, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        ent.get("name", ""),
                        ent.get("type", ""),
                        ent.get("confidence"),
                        mentions,
                        source_file,
                        self._now()
                    )
                )
                count += 1
            conn.commit()
        return count

    # ─── INSERCIÓN RELACIONES ────────────────────────────────────────────

    def insert_relations(self, relations: List[Dict[str, Any]], source_file: str) -> int:
        count = 0
        with self._get_conn() as conn:
            for rel in relations:
                conn.execute(
                    "INSERT INTO relations (subject, predicate, object, source_file, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        rel.get("subject", ""),
                        rel.get("predicate", ""),
                        rel.get("object", ""),
                        source_file,
                        self._now()
                    )
                )
                count += 1
            conn.commit()
        return count

    # ─── INSERCIÓN SÍNTESIS ──────────────────────────────────────────────

    def insert_synthesis(self, data: Dict[str, Any]) -> int:
        trends = json.dumps(data.get("trends", []), ensure_ascii=False)
        source_files = json.dumps(data.get("source_files", []), ensure_ascii=False)
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO syntheses (executive_summary, priority, trends, source_files, output_filename, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    data.get("executive_summary", ""),
                    data.get("priority"),
                    trends,
                    source_files,
                    data.get("output_filename"),
                    self._now()
                )
            )
            conn.commit()
            return cursor.lastrowid

    # ─── INSERCIÓN CLASIFICACIONES ───────────────────────────────────────

    def insert_classification(self, data: Dict[str, Any], source_file: str) -> int:
        tags = json.dumps(data.get("secondary_tags", []), ensure_ascii=False)
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO classifications (primary_category, secondary_tags, justification, source_file, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    data.get("primary_category", ""),
                    tags,
                    data.get("justification", ""),
                    source_file,
                    self._now()
                )
            )
            conn.commit()
            return cursor.lastrowid

    # ─── INSERCIÓN ACTION ITEMS ──────────────────────────────────────────

    def insert_action_items(self, items: List[Dict[str, Any]], source_synthesis: str) -> int:
        count = 0
        with self._get_conn() as conn:
            for item in items:
                conn.execute(
                    "INSERT INTO action_items (item_id, description, owner, deadline, priority, source_synthesis, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.get("id", ""),
                        item.get("description", ""),
                        item.get("owner", ""),
                        item.get("deadline", ""),
                        item.get("priority", ""),
                        source_synthesis,
                        self._now()
                    )
                )
                count += 1
            conn.commit()
        return count

    # ─── CONSULTAS ──────────────────────────────────────────────────────

    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Ejecuta una consulta SELECT y devuelve filas como diccionarios."""
        with self._get_conn() as conn:
            try:
                cursor = conn.execute(sql, params)
                columns = [desc[0] for desc in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
            except sqlite3.Error as exc:
                raise DatabaseError(f"Error en consulta: {exc}") from exc

    def list_entities(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM entities ORDER BY created_at DESC LIMIT ?", (limit,))

    def list_relations(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM relations ORDER BY created_at DESC LIMIT ?", (limit,))

    def list_syntheses(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM syntheses ORDER BY created_at DESC LIMIT ?", (limit,))

    def list_classifications(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM classifications ORDER BY created_at DESC LIMIT ?", (limit,))

    def list_action_items(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM action_items ORDER BY created_at DESC LIMIT ?", (limit,))

    def search_entities(self, name: str) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM entities WHERE name LIKE ? ORDER BY created_at DESC", (f"%{name}%",))

    def entities_by_type(self, entity_type: str) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM entities WHERE type = ? ORDER BY created_at DESC", (entity_type,))

    def stats(self) -> Dict[str, int]:
        """Devuelve conteos de todas las tablas."""
        with self._get_conn() as conn:
            tables = ["raw_news", "entities", "relations", "syntheses", "classifications", "action_items"]
            result = {}
            for table in tables:
                row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
                result[table] = row[0]
            return result
