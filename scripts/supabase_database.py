"""
supabase_database.py — SupabaseDatabase, reemplazo drop-in de PortalDatabase.
Usa service_role client (bypassea RLS) para operaciones de backend.
Mismas firmas que PortalDatabase para compatibilidad total.
"""

import json
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

from scripts.supabase_client import get_supabase_admin, use_supabase


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseDatabase:
    """
    Interfaz de base de datos sobre Supabase PostgreSQL.
    Mismos métodos que PortalDatabase + extras específicos de Supabase.
    """

    def __init__(self, client=None) -> None:
        self._client = client or get_supabase_admin()
        if self._client is None:
            raise RuntimeError("Supabase no configurado. Set SUPABASE_URL + SUPABASE_SERVICE_KEY.")

    # ─── RAW NEWS ────────────────────────────────────────────────────────

    def insert_raw_news(self, filename: str, content: str, checksum: str) -> int:
        data = {"filename": filename, "content": content, "checksum": checksum}
        result = self._client.table("raw_news").insert(data).execute()
        rows = result.data
        return rows[0]["id"] if rows else 0

    def insert_raw_news_full(
        self, filename: str, content: str, checksum: str,
        source: str = "", category: str = "", title: str = "",
        link: str = "", link_type: str = "", published: str = "",
    ) -> int:
        """Inserción completa con todos los metadatos."""
        data = {
            "filename": filename, "content": content, "checksum": checksum,
            "source": source, "category": category, "title": title,
            "link": link, "link_type": link_type, "published": published,
        }
        result = self._client.table("raw_news").insert(data).execute()
        rows = result.data
        return rows[0]["id"] if rows else 0

    def raw_news_exists(self, checksum: str) -> bool:
        result = self._client.table("raw_news").select("id").eq("checksum", checksum).execute()
        return len(result.data) > 0

    def mark_raw_news_processed(self, filenames: List[str]) -> int:
        count = 0
        for fn in filenames:
            result = self._client.table("raw_news").update(
                {"processed_at": _now()}
            ).eq("filename", fn).is_("processed_at", "null").execute()
            count += len(result.data)
        return count

    def get_unprocessed_filenames(self) -> List[str]:
        result = self._client.table("raw_news").select("filename").is_("processed_at", "null").order("ingested_at", desc=True).execute()
        return [r["filename"] for r in result.data]

    # ─── ENTIDADES ───────────────────────────────────────────────────────

    def insert_entities(self, entities: List[Dict[str, Any]], source_file: str) -> int:
        rows = []
        for ent in entities:
            mentions = ent.get("mentions", [])
            if isinstance(mentions, list):
                mentions = json.dumps(mentions, ensure_ascii=False)
            rows.append({
                "name": ent.get("name", ""),
                "type": ent.get("type", ""),
                "confidence": ent.get("confidence"),
                "mentions": mentions,
                "source_file": source_file,
            })
        if rows:
            self._client.table("entities").insert(rows).execute()
        return len(rows)

    # ─── RELACIONES ──────────────────────────────────────────────────────

    def insert_relations(self, relations: List[Dict[str, Any]], source_file: str) -> int:
        rows = []
        for rel in relations:
            rows.append({
                "subject": rel.get("subject", ""),
                "predicate": rel.get("predicate", ""),
                "object": rel.get("object", ""),
                "source_file": source_file,
            })
        if rows:
            self._client.table("relations").insert(rows).execute()
        return len(rows)

    # ─── SÍNTESIS ────────────────────────────────────────────────────────

    def insert_synthesis(self, data: Dict[str, Any]) -> int:
        trends = data.get("trends", [])
        if isinstance(trends, str):
            try:
                trends = json.loads(trends)
            except Exception:
                trends = [trends]
        source_files = data.get("source_files", [])
        if isinstance(source_files, str):
            try:
                source_files = json.loads(source_files)
            except Exception:
                source_files = [source_files]

        row = {
            "executive_summary": data.get("executive_summary", ""),
            "priority": data.get("priority"),
            "trends": json.dumps(trends, ensure_ascii=False),
            "source_files": json.dumps(source_files, ensure_ascii=False),
            "output_filename": data.get("output_filename"),
        }
        result = self._client.table("syntheses").insert(row).execute()
        rows = result.data
        return rows[0]["id"] if rows else 0

    # ─── CLASIFICACIONES ─────────────────────────────────────────────────

    def insert_classification(self, data: Dict[str, Any], source_file: str) -> int:
        tags = data.get("secondary_tags", [])
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = [tags]
        row = {
            "primary_category": data.get("primary_category", ""),
            "secondary_tags": json.dumps(tags, ensure_ascii=False),
            "justification": data.get("justification", ""),
            "source_file": source_file,
        }
        result = self._client.table("classifications").insert(row).execute()
        rows = result.data
        return rows[0]["id"] if rows else 0

    # ─── ACTION ITEMS ────────────────────────────────────────────────────

    def insert_action_items(self, items: List[Dict[str, Any]], source_synthesis: str) -> int:
        rows = []
        for item in items:
            rows.append({
                "item_id": item.get("id", ""),
                "description": item.get("description", ""),
                "owner": item.get("owner", ""),
                "deadline": item.get("deadline", ""),
                "priority": item.get("priority", ""),
                "source_synthesis": source_synthesis,
            })
        if rows:
            self._client.table("action_items").insert(rows).execute()
        return len(rows)

    # ─── CONSULTAS ───────────────────────────────────────────────────────

    def list_entities(self, limit: int = 50) -> List[Dict[str, Any]]:
        result = self._client.table("entities").select("*").order("created_at", desc=True).limit(limit).execute()
        return self._decode_jsonb(result.data, ["mentions"])

    def list_relations(self, limit: int = 50) -> List[Dict[str, Any]]:
        result = self._client.table("relations").select("*").order("created_at", desc=True).limit(limit).execute()
        return result.data

    def list_syntheses(self, limit: int = 20) -> List[Dict[str, Any]]:
        result = self._client.table("syntheses").select("*").order("created_at", desc=True).limit(limit).execute()
        return self._decode_jsonb(result.data, ["trends", "source_files"])

    def list_classifications(self, limit: int = 50) -> List[Dict[str, Any]]:
        result = self._client.table("classifications").select("*").order("created_at", desc=True).limit(limit).execute()
        return self._decode_jsonb(result.data, ["secondary_tags"])

    def list_action_items(self, limit: int = 50) -> List[Dict[str, Any]]:
        result = self._client.table("action_items").select("*").order("created_at", desc=True).limit(limit).execute()
        return result.data

    def search_entities(self, name: str) -> List[Dict[str, Any]]:
        result = self._client.table("entities").select("*").ilike("name", f"%{name}%").order("created_at", desc=True).execute()
        return self._decode_jsonb(result.data, ["mentions"])

    def entities_by_type(self, entity_type: str) -> List[Dict[str, Any]]:
        result = self._client.table("entities").select("*").eq("type", entity_type).order("created_at", desc=True).execute()
        return self._decode_jsonb(result.data, ["mentions"])

    def stats(self) -> Dict[str, int]:
        tables = ["raw_news", "entities", "relations", "syntheses", "classifications", "action_items"]
        result = {}
        for table in tables:
            r = self._client.table(table).select("id", count="exact").limit(0).execute()
            result[table] = r.count if hasattr(r, "count") and r.count is not None else len(r.data)
        return result

    def query(self, table: str, select: str = "*", filters: Optional[Dict] = None,
              order: Optional[str] = None, desc: bool = True, limit: int = 100) -> List[Dict[str, Any]]:
        """Consulta genérica con filtros."""
        q = self._client.table(table).select(select)
        if filters:
            for col, val in filters.items():
                q = q.eq(col, val)
        if order:
            q = q.order(order, desc=desc)
        q = q.limit(limit)
        result = q.execute()
        return result.data

    # ─── FEED CONFIGS ────────────────────────────────────────────────────

    def list_feed_configs(self) -> List[Dict[str, Any]]:
        result = self._client.table("feed_configs").select("*").order("created_at", desc=True).execute()
        return result.data

    def upsert_feed_config(self, name: str, url: str, category: str = "Otro",
                           enabled: bool = True, poll_interval_min: int = 30) -> Dict[str, Any]:
        # Buscar existente por nombre
        existing = self._client.table("feed_configs").select("id").eq("name", name).execute()
        data = {"name": name, "url": url, "category": category, "enabled": enabled, "poll_interval_min": poll_interval_min}
        if existing.data:
            result = self._client.table("feed_configs").update(data).eq("name", name).execute()
        else:
            result = self._client.table("feed_configs").insert(data).execute()
        return result.data[0] if result.data else {}

    def delete_feed_config(self, name: str) -> bool:
        result = self._client.table("feed_configs").delete().eq("name", name).execute()
        return len(result.data) > 0

    # ─── SYSTEM STATE ────────────────────────────────────────────────────

    def get_state(self, key: str) -> Optional[Dict[str, Any]]:
        result = self._client.table("system_state").select("value").eq("key", key).execute()
        if result.data:
            return result.data[0]["value"]
        return None

    def set_state(self, key: str, value: Dict[str, Any]) -> None:
        existing = self._client.table("system_state").select("key").eq("key", key).execute()
        data = {"key": key, "value": value, "updated_at": _now()}
        if existing.data:
            self._client.table("system_state").update({"value": value, "updated_at": _now()}).eq("key", key).execute()
        else:
            self._client.table("system_state").insert(data).execute()

    # ─── USER CREDENTIALS ────────────────────────────────────────────────

    def get_user_credential(self, user_id: str, provider: str) -> Optional[str]:
        result = self._client.table("user_credentials").select("api_key").eq("user_id", user_id).eq("provider", provider).execute()
        if result.data:
            return result.data[0]["api_key"]
        return None

    def set_user_credential(self, user_id: str, provider: str, api_key: str) -> None:
        existing = self._client.table("user_credentials").select("id").eq("user_id", user_id).eq("provider", provider).execute()
        if existing.data:
            self._client.table("user_credentials").update({"api_key": api_key}).eq("user_id", user_id).eq("provider", provider).execute()
        else:
            self._client.table("user_credentials").insert({"user_id": user_id, "provider": provider, "api_key": api_key}).execute()

    def list_user_credentials(self, user_id: str) -> List[Dict[str, Any]]:
        result = self._client.table("user_credentials").select("provider,api_key").eq("user_id", user_id).execute()
        return result.data

    def delete_user_credential(self, user_id: str, provider: str) -> bool:
        result = self._client.table("user_credentials").delete().eq("user_id", user_id).eq("provider", provider).execute()
        return len(result.data) > 0

    # ─── CHAT MESSAGES ───────────────────────────────────────────────────

    def insert_chat_message(self, user_id: str, role: str, content: str,
                           context_files: Optional[List[str]] = None) -> int:
        data = {
            "user_id": user_id, "role": role, "content": content,
            "context_files": json.dumps(context_files or [], ensure_ascii=False),
        }
        result = self._client.table("chat_messages").insert(data).execute()
        return result.data[0]["id"] if result.data else 0

    def list_chat_messages(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        result = self._client.table("chat_messages").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute()
        return self._decode_jsonb(result.data, ["context_files"])

    # ─── HELPERS ─────────────────────────────────────────────────────────

    @staticmethod
    def _decode_jsonb(rows: List[Dict[str, Any]], columns: List[str]) -> List[Dict[str, Any]]:
        """Decodifica campos JSONB que vienen como string desde Supabase."""
        for row in rows:
            for col in columns:
                if col in row and isinstance(row[col], str):
                    try:
                        row[col] = json.loads(row[col])
                    except (json.JSONDecodeError, TypeError):
                        pass
        return rows
