"""Portal Pi: orquestador multi-IA con convergencia controlada.

Integración rápida con el flujo actual:

    from portal_pi_orchestrator import Orchestrator, ProviderAdapter

    # Tu LLMClient actual debe exponer call_json(system, prompt).
    adapter = ProviderAdapter(call_json=llm_client.call_json, name="portal-llm")
    result = Orchestrator(adapter).run(
        objective="Analiza las noticias del lote y extrae conclusiones accionables",
        inputs=[{"id": "art-001", "text": texto, "source": url}],
    )

El módulo no hace llamadas de red ni depende de ningún proveedor. Solo necesita
un callable que reciba (system_prompt, user_prompt) y devuelva un dict JSON.

NOTA DE COMPATIBILIDAD:
    Si tu call_json devuelve un wrapper tipo {status, data, audit_note},
    usa UnwrapAdapter para desempaquetar automáticamente:

    adapter = ProviderAdapter(call_json=UnwrapAdapter(llm_client.call_json), name="portal-llm")
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

Json = Dict[str, Any]
CallJSON = Callable[[str, str], Json]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class ProviderAdapter:
    """Adaptador mínimo para tu llm_client.py existente."""

    call_json: CallJSON
    name: str = "provider"

    def ask(self, system: str, prompt: str) -> Json:
        response = self.call_json(system, prompt)
        if not isinstance(response, dict):
            raise ValueError(f"{self.name}: la respuesta no es un objeto JSON")
        return response


@dataclass
class Claim:
    text: str
    evidence_ids: List[str] = field(default_factory=list)
    confidence: float = 0.0
    status: str = "supported"  # supported | disputed | rejected | needs_review
    reason: str = ""


@dataclass
class AgentArtifact:
    role: str
    objective: str
    claims: List[Claim] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    score: float = 0.0
    raw: Json = field(default_factory=dict)

    def to_dict(self) -> Json:
        data = asdict(self)
        data["claims"] = [asdict(c) for c in self.claims]
        return data


@dataclass
class OrchestrationResult:
    objective: str
    status: str
    answer: str
    claims: List[Claim]
    evidence: List[Json]
    contradictions: List[str]
    next_actions: List[str]
    quality: Json
    trace: List[Json]
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Json:
        data = asdict(self)
        data["claims"] = [asdict(c) for c in self.claims]
        return data


class Orchestrator:
    """Supervisor + análisis independiente + crítico + juez.

    Principios deliberadamente estrictos:
    - Los agentes no conversan libremente: intercambian artefactos JSON.
    - Toda afirmación debe apuntar a evidence_ids.
    - El crítico intenta refutar, no adornar.
    - El juez puede rechazar, pedir revisión o cerrar.
    - Máximo de rondas y umbral de calidad para evitar bucles y ruido.
    """

    SYSTEM = (
        "Eres un componente de un sistema de análisis. Sé preciso, breve y adversarial. "
        "No inventes hechos ni fuentes. Si la evidencia no alcanza, usa needs_review. "
        "Responde únicamente JSON válido, sin markdown."
    )

    def __init__(
        self,
        adapter: ProviderAdapter,
        *,
        max_rounds: int = 2,
        min_quality: float = 0.82,
        max_claims: int = 12,
    ) -> None:
        self.adapter = adapter
        self.max_rounds = max(1, min(max_rounds, 4))
        self.min_quality = max(0.0, min(min_quality, 1.0))
        self.max_claims = max(1, max_claims)

    def run(self, objective: str, inputs: Iterable[Json]) -> OrchestrationResult:
        evidence = self._normalize_inputs(inputs)
        if not objective.strip():
            raise ValueError("objective no puede estar vacío")
        if not evidence:
            raise ValueError("se necesita al menos una entrada con evidencia")

        trace: List[Json] = []
        plan = self._plan(objective, evidence)
        trace.append({"stage": "plan", "artifact": plan})

        proposals = [
            self._researcher(objective, evidence, focus="hechos y señales explícitas"),
            self._researcher(objective, evidence, focus="riesgos, ausencias y contraejemplos"),
        ]
        trace.append({"stage": "independent_analysis", "artifacts": [p.to_dict() for p in proposals]})

        current = proposals
        final: Optional[Json] = None
        for round_no in range(1, self.max_rounds + 1):
            critique = self._critic(objective, evidence, current)
            trace.append({"stage": "critique", "round": round_no, "artifact": critique})
            final = self._judge(objective, evidence, current, critique)
            trace.append({"stage": "judge", "round": round_no, "artifact": final})

            quality = self._quality(final)
            if quality["score"] >= self.min_quality or final.get("decision") == "reject":
                break

            # Solo se itera con instrucciones de corrección concretas.
            current = [self._revise(objective, evidence, artifact, critique) for artifact in current]
            trace.append({"stage": "revision", "round": round_no, "artifacts": [a.to_dict() for a in current]})

        final = final or {"decision": "needs_review", "answer": "Sin veredicto"}
        quality = self._quality(final)
        claims = self._claims_from(final.get("claims", []))[: self.max_claims]
        status = "accepted" if quality["score"] >= self.min_quality else "needs_review"
        if final.get("decision") == "reject":
            status = "rejected"

        return OrchestrationResult(
            objective=objective,
            status=status,
            answer=str(final.get("answer", "")),
            claims=claims,
            evidence=evidence,
            contradictions=[str(x) for x in final.get("contradictions", [])],
            next_actions=[str(x) for x in final.get("next_actions", [])],
            quality=quality,
            trace=trace,
        )

    def _normalize_inputs(self, inputs: Iterable[Json]) -> List[Json]:
        normalized = []
        for index, item in enumerate(inputs):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            source = str(item.get("source", "")).strip()
            item_id = str(item.get("id") or stable_id("evidence", f"{index}:{source}:{text}"))
            normalized.append({"id": item_id, "source": source, "text": text[:12000]})
        return normalized

    def _plan(self, objective: str, evidence: List[Json]) -> Json:
        prompt = f"""OBJETIVO: {objective}\n\nEVIDENCIA DISPONIBLE:\n{compact_json(evidence)}\n\nDevuelve {{\"scope\": [string], \"out_of_scope\": [string], \"success_criteria\": [string], \"questions\": [string]}}. Mantén el foco: no propongas tareas fuera del objetivo."""
        return self.adapter.ask(self.SYSTEM, prompt)

    def _researcher(self, objective: str, evidence: List[Json], focus: str) -> AgentArtifact:
        prompt = f"""OBJETIVO: {objective}\nENFOQUE: {focus}\n\nEVIDENCIA:\n{compact_json(evidence)}\n\nDevuelve {{\"claims\": [{{\"text\": string, \"evidence_ids\": [string], \"confidence\": number, \"reason\": string}}], \"gaps\": [string], \"recommendations\": [string]}}. Cada claim debe citar uno o más IDs reales de evidencia."""
        raw = self.adapter.ask(self.SYSTEM, prompt)
        return self._artifact("researcher", objective, raw)

    def _critic(self, objective: str, evidence: List[Json], artifacts: List[AgentArtifact]) -> Json:
        packet = [a.to_dict() for a in artifacts]
        prompt = f"""OBJETIVO: {objective}\n\nEVIDENCIA:\n{compact_json(evidence)}\n\nPROPUESTAS INDEPENDIENTES:\n{compact_json(packet)}\n\nActúa como crítico hostil. Busca claims sin evidencia, contradicciones, saltos lógicos, duplicados y salidas fuera de foco. Devuelve {{\"accepted_claims\": [string], \"disputed_claims\": [string], \"rejected_claims\": [string], \"missing_evidence\": [string], \"corrections\": [string], \"score\": number}}."""
        return self.adapter.ask(self.SYSTEM, prompt)

    def _judge(self, objective: str, evidence: List[Json], artifacts: List[AgentArtifact], critique: Json) -> Json:
        prompt = f"""OBJETIVO: {objective}\n\nEVIDENCIA:\n{compact_json(evidence)}\n\nANÁLISIS:\n{compact_json([a.to_dict() for a in artifacts])}\n\nCRÍTICA:\n{compact_json(critique)}\n\nEres el juez final. Conserva solo afirmaciones defendibles y relevantes. Devuelve {{\"decision\": \"accept|needs_review|reject\", \"answer\": string, \"claims\": [{{\"text\": string, \"evidence_ids\": [string], \"confidence\": number, \"status\": \"supported|disputed|rejected|needs_review\", \"reason\": string}}], \"contradictions\": [string], \"next_actions\": [string], \"quality_score\": number}}. No rellenes huecos con imaginación."""
        return self.adapter.ask(self.SYSTEM, prompt)

    def _revise(self, objective: str, evidence: List[Json], artifact: AgentArtifact, critique: Json) -> AgentArtifact:
        prompt = f"""OBJETIVO: {objective}\nEVIDENCIA: {compact_json(evidence)}\nTU PROPUESTA: {compact_json(artifact.to_dict())}\nCRÍTICA: {compact_json(critique)}\n\nCorrige solo lo señalado. Elimina claims no demostrables y conserva la trazabilidad. Devuelve el mismo esquema de claims, gaps y recommendations."""
        raw = self.adapter.ask(self.SYSTEM, prompt)
        return self._artifact("researcher_revised", objective, raw)

    def _artifact(self, role: str, objective: str, raw: Json) -> AgentArtifact:
        claims = self._claims_from(raw.get("claims", []))
        return AgentArtifact(
            role=role,
            objective=objective,
            claims=claims,
            contradictions=[str(x) for x in raw.get("contradictions", [])],
            recommendations=[str(x) for x in raw.get("recommendations", [])],
            gaps=[str(x) for x in raw.get("gaps", [])],
            score=float(raw.get("score", 0.0) or 0.0),
            raw=raw,
        )

    def _claims_from(self, values: Any) -> List[Claim]:
        if not isinstance(values, list):
            return []
        claims = []
        for value in values:
            if isinstance(value, str):
                claims.append(Claim(text=value, status="needs_review"))
                continue
            if not isinstance(value, dict) or not str(value.get("text", "")).strip():
                continue
            evidence_ids = value.get("evidence_ids", [])
            if not isinstance(evidence_ids, list):
                evidence_ids = []
            claims.append(Claim(
                text=str(value["text"]).strip(),
                evidence_ids=[str(x) for x in evidence_ids],
                confidence=max(0.0, min(float(value.get("confidence", 0.0) or 0.0), 1.0)),
                status=str(value.get("status", "supported")),
                reason=str(value.get("reason", "")),
            ))
        return claims

    def _quality(self, final: Json) -> Json:
        score = float(final.get("quality_score", 0.0) or 0.0)
        claims = self._claims_from(final.get("claims", []))
        traceable = sum(bool(c.evidence_ids) for c in claims)
        traceability = traceable / len(claims) if claims else 0.0
        contradiction_penalty = min(len(final.get("contradictions", [])) * 0.05, 0.25)
        effective = max(0.0, min(score * 0.65 + traceability * 0.35 - contradiction_penalty, 1.0))
        return {
            "score": round(effective, 3),
            "model_score": round(max(0.0, min(score, 1.0)), 3),
            "traceability": round(traceability, 3),
            "contradiction_penalty": round(contradiction_penalty, 3),
            "claim_count": len(claims),
        }


if __name__ == "__main__":
    print("Módulo listo. Importa Orchestrator y conecta ProviderAdapter con tu llm_client.call_json().")


def UnwrapAdapter(call_json_wrapped: Callable[[str, str], Json]) -> Callable[[str, str], Json]:
    """Adaptador para LLMClient.call_json que devuelve {status, data, audit_note}.

    El Orchestrator espera que call_json devuelva el JSON tal cual lo genera el LLM,
    pero Portal Pi envuelve las respuestas en {status: ok, data: {...}, audit_note: "..."}.

    Este adaptador desempaqueta el wrapper para que el orquestador reciba el contenido
    real del LLM sin la capa de normalización de Portal Pi.

    Uso:
        adapter = ProviderAdapter(
            call_json=UnwrapAdapter(llm_client.call_json),
            name="portal-llm",
        )
    """
    def _unwrap(system: str, prompt: str) -> Json:
        result = call_json_wrapped(system, prompt)
        # Si tiene el wrapper {status, data}, devolver data directamente
        if isinstance(result, dict) and "status" in result and "data" in result:
            return result["data"]
        # Si no tiene wrapper, devolver tal cual
        return result
    return _unwrap
