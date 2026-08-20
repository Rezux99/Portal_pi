"""
multi_agent.py
Orquestador multi-agente para Portal Pi.

Cada paso del pipeline se asigna al modelo más adecuado según su rol.
Los agentes se critican entre sí para triangular resultados.

Arquitectura:
  Extractor (rápido) → Analista (razonamiento) → Crítico (validación) → Sintetizador (escritura)
       ↓                    ↓                        ↓                       ↓
    [groq/cerebras]    [gemini/nvidia]         [gemini/nvidia]         [gemini/nvidia]
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

from scripts.llm_client import LLMClient, LLMClientError, parse_json_response
from scripts.synergy_router import is_valid_json as synergy_is_valid_json, is_non_empty
from scripts.state_manager import StateManager
from scripts.database import PortalDatabase
from scripts.report_generator import generate_report


from scripts.paths import BASE_DIR, log_to_file


# ─── PROMPTS POR ROL ──────────────────────────────────────────────────────

ROLE_PROMPTS = {
    "extractor": {
        "system": (
            "Eres un analista de inteligencia especializado en extracción de datos. "
            "Tu trabajo es leer noticias y extraer SOLO hechos verificables: entidades (personas, organizaciones, lugares, tecnologías), "
            "relaciones entre ellas, y datos duros (cifras, fechas, lugares). "
            "NO interpretes, NO opines, NO infieras. Extrae solo lo que está explícitamente en el texto. "
            "Responde SOLO en JSON válido."
        ),
        "schema": {
            "status": "ok",
            "data": {
                "source_file": "<filename>",
                "output_filename": "entities.json",
                "entities": [{"name": "", "type": "PERSON|ORGANIZATION|LOCATION|TECHNOLOGY|EVENT|CONCEPT", "confidence": 0.0, "mentions": []}],
                "relations": [{"subject": "", "predicate": "", "object": ""}]
            },
            "audit_note": ""
        }
    },
    "analyst": {
        "system": (
            "Eres un analista de inteligencia senior. Tu trabajo es analizar el contexto, detectar sesgos, "
            "identificar patrones y evaluar la fiabilidad de la información. "
            "Recibes entidades extraídas por otro agente. Tu trabajo es: "
            "1) Clasificar la temática y asignar tags. "
            "2) Evaluar si hay sesgos evidentes en las fuentes. "
            "3) Identificar el nivel de prioridad (ALTA/MEDIA/BAJA) y por qué. "
            "4) Detectar tendencias o patrones. "
            "Responde SOLO en JSON válido."
        ),
        "schema": {
            "status": "ok",
            "data": {
                "primary_category": "",
                "secondary_tags": [],
                "priority": "ALTA|MEDIA|BAJA",
                "priority_reason": "",
                "bias_analysis": "",
                "reliability_score": 0.0,
                "justification": ""
            },
            "audit_note": ""
        }
    },
    "critic": {
        "system": (
            "Eres un crítico de inteligencia — el 'abogado del diablo'. "
            "Recibes los resultados de un extractor y un analista. Tu trabajo es: "
            "1) Buscar contradicciones o inconsistencias entre lo extraído y lo analizado. "
            "2) Identificar puntos ciegos: ¿qué falta? ¿qué no se mencionó pero debería? "
            "3) Cuestionar las afirmaciones más fuertes. ¿Son realmente sólidas? "
            "4) Señalar si la confianza asignada es demasiado alta o baja. "
            "5) Generar preguntas que el equipo debería investigar. "
            "Sé constructivo pero riguroso. Responde SOLO en JSON válido."
        ),
        "schema": {
            "status": "ok",
            "data": {
                "contradictions": [],
                "blind_spots": [],
                "overconfident_claims": [],
                "questions_to_investigate": [],
                "overall_assessment": "SOLID|MODERATE|WEAK",
                "critique_summary": ""
            },
            "audit_note": ""
        }
    },
    "synthesizer": {
        "system": (
            "Eres un sintetizador de inteligencia. Recibes: "
            "1) Entidades y relaciones extraídas. "
            "2) Análisis de contexto, sesgos y prioridad. "
            "3) Crítica con puntos ciegos y contradicciones. "
            "Tu trabajo es generar un informe ejecutivo que: "
            "- Sintetice los hallazgos en 3-5 frases claras. "
            "- Integre la crítica (menciona los puntos ciegos identificados). "
            "- Proponga acciones concretas. "
            "- Sea útil para alguien que necesita tomar decisiones. "
            "Responde SOLO en JSON válido."
        ),
        "schema": {
            "status": "ok",
            "data": {
                "executive_summary": "",
                "priority": "ALTA|MEDIA|BAJA",
                "trends": [],
                "blind_spots_acknowledged": [],
                "action_items": [{"id": "ACT-001", "description": "", "owner": "", "deadline": "", "priority": "ALTA|MEDIA|BAJA"}],
                "output_filename": "synthesis.json",
                "source_files": []
            },
            "audit_note": ""
        }
    }
}


class MultiAgentOrchestrator:
    """
    Orquestador que asigna modelos a roles y ejecuta el pipeline multi-agente.
    Cada agente ve el output del anterior, creando una cadena de razonamiento.
    """

    def __init__(self, llm_client: Optional[LLMClient] = None) -> None:
        self.llm = llm_client or LLMClient()
        self._log_entries: List[Dict[str, Any]] = []

    def _log(self, msg: str, level: str = "INFO") -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": msg,
        }
        self._log_entries.append(entry)
        print(f"  [{level}] {msg}")

    def _get_model_for_role(self, role: str) -> str:
        """Selecciona el mejor modelo disponible para un rol."""
        config = self.llm.config
        roles_config = config.get("roles", {})
        role_config = roles_config.get(role, {})
        preferred = role_config.get("preferred", [])

        # Modelos con key configurada
        available = self.llm._available_providers()

        # Intentar con los preferidos del rol
        for model in preferred:
            if model in available:
                return model

        # Fallback: cualquiera disponible
        if available:
            return available[0]

        raise LLMClientError(f"No hay modelos disponibles para el rol '{role}'")

    def _call_role(self, role: str, user_prompt: str, use_synergy: bool = False) -> Dict[str, Any]:
        """Llama al LLM con el prompt de un rol específico.

        Args:
            role: Nombre del rol (extractor, analyst, critic, synthesizer).
            user_prompt: Prompt del usuario.
            use_synergy: Si True, usa sinergia con validación JSON.
                        El proveedor rápido intenta, y si falla el formato JSON,
                        un corrector lo arregla automáticamente.
        """
        role_config = ROLE_PROMPTS.get(role)
        if not role_config:
            raise ValueError(f"Rol desconocido: {role}")

        system_prompt = role_config["system"]
        schema = role_config["schema"]
        schema_str = json.dumps(schema, indent=2, ensure_ascii=False)

        # Añadir schema al system prompt
        full_system = f"{system_prompt}\n\nSchema obligatorio de respuesta:\n{schema_str}"

        model_name = self._get_model_for_role(role)
        self._log(f"[{role}] → modelo: {model_name}{' (sinergia)' if use_synergy else ''}")

        # Forzar el modelo preferido
        old_preferred = self.llm._preferred_provider
        self.llm._preferred_provider = model_name

        try:
            if use_synergy:
                # Usar sinergia con validación JSON
                # Si el proveedor rápido devuelve JSON roto, el corrector lo arregla
                from scripts.synergy_router import is_valid_json_with_fields
                validator = is_valid_json_with_fields("status", "data")
                result = self.llm.call_with_synergy(full_system, user_prompt, validator=validator)
                raw = result.value
                self._log(f"[{role}] ✓ sinergia completada (fase: {result.phase}, proveedor: {result.provider})")
            else:
                raw = self.llm.call(full_system, user_prompt)

            result = parse_json_response(raw)
            self._log(f"[{role}] ✓ respondió ({len(raw)} chars)")
            return result
        except LLMClientError as e:
            self._log(f"[{role}] ✗ falló: {e}", "WARN")
            # Restaurar y reintentar sin preferencia
            self.llm._preferred_provider = None
            raw = self.llm.call(full_system, user_prompt)
            result = parse_json_response(raw)
            self._log(f"[{role}] ✓ respondió con fallback ({len(raw)} chars)")
            return result
        finally:
            self.llm._preferred_provider = old_preferred

    def run_pipeline(self, state_mgr: StateManager, db: PortalDatabase) -> Dict[str, Any]:
        """
        Ejecuta el pipeline multi-agente completo.

        Flujo:
        1. Extractor → entidades y relaciones (modelo rápido)
        2. Analista → contexto, sesgos, clasificación (modelo con razonamiento)
        3. Crítico → valida y cuestiona los resultados (modelo con razonamiento)
        4. Sintetizador → informe final integrando todo (modelo equilibrado)

        Returns: dict con resultados de cada agente y el informe generado.
        """
        from scripts.main import read_raw_news_files, route_response, assemble_dynamic_prompt

        results = {}
        raw_files = read_raw_news_files(limit=3)

        if not raw_files:
            return {"status": "error", "error": "No hay artículos raw para procesar"}

        self._log(f"Pipeline multi-agente iniciado — {len(raw_files)} artículos raw")

        # ── PASO 1: EXTRACTOR (sinergia: JSON estructurado crítico) ────
        try:
            state = state_mgr.get_full_state()
            prompt = assemble_dynamic_prompt(state, "/extract_entities")
            extraction = self._call_role("extractor", prompt, use_synergy=True)
            route_response(state_mgr, "/extract_entities", extraction)
            results["extraction"] = {"status": "ok", "model": self._get_model_for_role("extractor")}
        except Exception as e:
            results["extraction"] = {"status": "error", "error": str(e)}
            self._log(f"Extractor falló: {e}", "ERROR")

        # ── PASO 2: ANALISTA (sinergia: JSON con clasificación precisa) ──
        try:
            state = state_mgr.get_full_state()
            prompt = assemble_dynamic_prompt(state, "/classify_topic")
            # Inyectar contexto del extractor
            if results.get("extraction", {}).get("status") == "ok":
                extraction_summary = json.dumps(extraction.get("data", {}), ensure_ascii=False)[:1500]
                prompt += f"\n\n[RESULTADO_DEL_EXTRACTOR]\n{extraction_summary}"
            analysis = self._call_role("analyst", prompt, use_synergy=True)
            # Guardar clasificación
            classify_response = {"status": "ok", "data": analysis.get("data", {}), "audit_note": analysis.get("audit_note", "")}
            route_response(state_mgr, "/classify_topic", classify_response)
            results["analysis"] = {"status": "ok", "model": self._get_model_for_role("analyst")}
        except Exception as e:
            results["analysis"] = {"status": "error", "error": str(e)}
            self._log(f"Analista falló: {e}", "ERROR")

        # ── PASO 3: CRÍTICO (sinergia: JSON con validación cruzada) ──────
        try:
            # El crítico ve TODO lo anterior
            critique_context = f"""
[ENTIDADES EXTRAÍDAS]
{json.dumps(extraction.get('data', {}), ensure_ascii=False)[:2000]}

[ANÁLISIS DEL ANALISTA]
{json.dumps(analysis.get('data', {}), ensure_ascii=False)[:2000]}
"""
            state = state_mgr.get_full_state()
            prompt = assemble_dynamic_prompt(state, "/extract_entities")  # usa raw news como base
            prompt += f"\n\n{critique_context}"
            critique = self._call_role("critic", prompt, use_synergy=True)
            results["critique"] = {"status": "ok", "data": critique.get("data", {}), "model": self._get_model_for_role("critic")}
        except Exception as e:
            results["critique"] = {"status": "error", "error": str(e)}
            self._log(f"Crítico falló: {e}", "ERROR")

        # ── PASO 4: SINTETIZADOR (sinergia: JSON con informe ejecutivo) ──
        try:
            synthesis_context = f"""
[ENTIDADES EXTRAÍDAS]
{json.dumps(extraction.get('data', {}), ensure_ascii=False)[:1500]}

[ANÁLISIS]
{json.dumps(analysis.get('data', {}), ensure_ascii=False)[:1500]}

[CRÍTICA DEL CRÍTICO]
{json.dumps(results.get('critique', {}).get('data', {}), ensure_ascii=False)[:1500]}
"""
            state = state_mgr.get_full_state()
            prompt = assemble_dynamic_prompt(state, "/synthesize_news")
            prompt += f"\n\n{synthesis_context}"
            synthesis = self._call_role("synthesizer", prompt, use_synergy=True)

            # Guardar síntesis
            synth_data = synthesis.get("data", {})
            synth_response = {
                "status": "ok",
                "data": {
                    "executive_summary": synth_data.get("executive_summary", ""),
                    "priority": synth_data.get("priority"),
                    "trends": synth_data.get("trends", []),
                    "source_files": [r.get("filename", "") for r in raw_files],
                    "output_filename": "synthesis.json",
                },
                "audit_note": synthesis.get("audit_note", "")
            }
            route_response(state_mgr, "/synthesize_news", synth_response)

            # Guardar action items si el sintetizador los generó
            action_items = synth_data.get("action_items", [])
            if action_items:
                from scripts.main import _versioned_filename
                action_response = {
                    "status": "ok",
                    "data": {
                        "action_items": action_items,
                        "output_filename": "actions.json",
                        "source_file": raw_files[0].get("filename", ""),
                    },
                    "audit_note": "Generado por el sintetizador multi-agente."
                }
                route_response(state_mgr, "/generate_action_items", action_response)

            results["synthesis"] = {"status": "ok", "model": self._get_model_for_role("synthesizer")}
        except Exception as e:
            results["synthesis"] = {"status": "error", "error": str(e)}
            self._log(f"Sintetizador falló: {e}", "ERROR")

        # ── PASO 5: GENERAR INFORME ────────────────────────────────────
        try:
            report_path = generate_report()
            results["report"] = {"status": "ok", "filename": report_path.name}
        except Exception as e:
            results["report"] = {"status": "error", "error": str(e)}

        # ── Resumen ────────────────────────────────────────────────────
        ok_count = sum(1 for v in results.values() if v.get("status") == "ok")
        total = len(results)
        results["summary"] = f"{ok_count}/{total} pasos completados"
        results["log"] = self._log_entries

        self._log(f"Pipeline completado: {results['summary']}")

        return results
