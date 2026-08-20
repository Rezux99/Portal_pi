"""
synergy_router.py — Portal Pi
==============================
Orquestador sinérgico de llamadas LLM con validación y corrección automática.

Flujo sinérgico:
  1. Proveedor rápido (speed_tier 1) genera un borrador.
  2. Si el validador acepta → retorno inmediato (costo mínimo).
  3. Si el validador rechaza → el borrador defectuoso se pasa como contexto
     a un proveedor corrector (speed_tier 2-3), que lo corrige.
  4. Si el proveedor rápido está caído → failover directo al corrector.

Este módulo NO reemplaza SmartRouter: lo usa como motor de ejecución.
SmartRouter gestiona circuit breakers, telemetría y scoring.
SynergyRouter añade la capa de validación + corrección encima.

Uso directo:
    from scripts.synergy_router import SynergyRouter, SynergyConfig
    from scripts.llm_client import LLMClient

    llm = LLMClient()
    router = SynergyRouter(llm)

    # Con validación JSON
    result = await router.execute(
        system_prompt="Genera un JSON con entidades",
        user_prompt="Analiza este texto...",
        validator=is_valid_json,
    )

    # Sin validación (failover inteligente)
    result = await router.execute(
        system_prompt="Resume las noticias",
        user_prompt="...",
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from scripts.smart_router import SmartRouter, Target, RouteResult, AllTargetsDownError

logger = logging.getLogger("synergy_router")


# ─── EXCEPCIONES ──────────────────────────────────────────────────────────

class SynergyRouterError(Exception):
    """Error base del módulo."""


class AllProvidersFailedError(SynergyRouterError):
    """Todos los proveedores fallaron o devolvieron resultados inválidos."""

    def __init__(self, attempts: list[SynergyAttempt]):
        self.attempts = attempts
        summary = "; ".join(f"{a.provider}: {a.phase} ({a.error or 'validation failed'})" for a in attempts)
        super().__init__(f"Sinergia fallida tras todos los intentos: {summary}")


# ─── SPEED TIER ────────────────────────────────────────────────────────────

class SpeedTier(Enum):
    """Categorías de velocidad de proveedor."""
    FAST = 1       # Groq, Cerebras — latencia < 500ms
    MEDIUM = 2     # Gemini Flash, Nvidia — latencia 500ms-2s
    SLOW = 3       # Modal/GLM, OpenRouter — latencia > 2s


# ─── DATOS ────────────────────────────────────────────────────────────────

@dataclass
class SynergyAttempt:
    """Registro de un intento individual dentro de la sinergia."""
    provider: str
    phase: str          # "draft" | "correction" | "failover"
    success: bool
    latency_ms: float
    output_chars: int = 0
    error: Optional[str] = None
    validated: Optional[bool] = None


@dataclass
class SynergyResult:
    """Resultado completo de una ejecución sinérgica."""
    value: str
    provider: str
    phase: str              # "draft" (aceptado directo) | "correction" (corregido) | "failover" (sin borrador previo)
    synergy_used: bool      # True si se necesitó corrección
    validated: bool         # True si pasó el validador
    attempts: list[SynergyAttempt] = field(default_factory=list)
    total_latency_ms: float = 0.0

    @property
    def is_degraded(self) -> bool:
        """True si la respuesta no es del proveedor rápido ideal."""
        return self.phase != "draft"


# ─── VALIDADORES COMUNES ──────────────────────────────────────────────────

def is_valid_json(text: str) -> bool:
    """Valida que el texto sea JSON parseable."""
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, TypeError):
        return False


def is_valid_json_with_fields(*required_fields: str) -> Callable[[str], bool]:
    """Factory de validador que exige campos obligatorios en el JSON."""
    def validator(text: str) -> bool:
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return False
            return all(f in data for f in required_fields)
        except (json.JSONDecodeError, TypeError):
            return False
    validator.__name__ = f"is_valid_json_with_fields({', '.join(required_fields)})"
    return validator


def is_non_empty(text: str) -> bool:
    """Valida que la respuesta no esté vacía ni sea un error genérico."""
    cleaned = text.strip()
    if not cleaned:
        return False
    # Patrones comunes de respuestas degradadas
    degraded_patterns = [
        "lo siento, no puedo",
        "i'm sorry, i cannot",
        "as an ai",
        "como modelo de ia",
        "todos los proveedores",
    ]
    return not any(p in cleaned.lower() for p in degraded_patterns)


def always_valid(text: str) -> bool:
    """Sin validación — acepta cualquier respuesta."""
    return True


# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────

@dataclass
class SynergyConfig:
    """Configuración del SynergyRouter."""
    # Orden de proveedores rápidos para borrador (speed_tier 1)
    fast_providers: list[str] = field(default_factory=lambda: ["groq", "cerebras"])
    # Orden de proveedores correctores (speed_tier 2+)
    correction_providers: list[str] = field(default_factory=lambda: ["gemini_flash", "nvidia", "modal", "openrouter_free", "together"])
    # Timeout por intento individual (segundos)
    per_attempt_timeout_s: float = 15.0
    # Máximo de intentos de corrección antes de rendirse
    max_correction_attempts: int = 2
    # Si True, intenta el siguiente proveedor rápido si el primero falla (antes de corrección)
    try_all_fast_first: bool = True


# ─── SYNERGY ROUTER ───────────────────────────────────────────────────────

class SynergyRouter:
    """
    Orquestador sinérgico que usa SmartRouter como motor de ejecución.

    Añade sobre SmartRouter:
    - Validación de output con callback configurable
    - Corrección automática: el output defectuoso de un proveedor rápido
      se pasa como contexto a un proveedor corrector
    - Failover inteligente: si el rápido está caído, va directo al corrector
    - Telemetría de sinergia: qué proveedor se usó, si hubo corrección, etc.

    El costo extra de múltiples proveedores solo se paga si la calidad lo requiere.
    """

    def __init__(self, llm_client: Any, config: SynergyConfig | None = None):
        """
        Args:
            llm_client: Instancia de LLMClient con SmartRouter configurado.
            config: Configuración de sinergia. Si es None, usa defaults.
        """
        self.llm = llm_client
        self.config = config or SynergyConfig()
        self._synergy_log: list[dict[str, Any]] = []

    # ── EJECUCIÓN PRINCIPAL ────────────────────────────────────────────

    async def execute_async(
        self,
        system_prompt: str,
        user_prompt: str,
        validator: Callable[[str], bool] | None = None,
        opts: dict[str, Any] | None = None,
    ) -> SynergyResult:
        """
        Ejecución sinérgica asíncrona.

        Flujo:
        1. Intento rápido (proveedor speed_tier 1).
        2. Si pasa validación → retorno inmediato.
        3. Si falla validación → corrección con proveedor más capaz.
        4. Si el rápido está caído → failover directo al corrector.

        Args:
            system_prompt: Prompt del sistema.
            user_prompt: Prompt del usuario.
            validator: Función que devuelve True si el output es válido.
                      Por defecto acepta todo (always_valid).
            opts: Opciones adicionales (timeout, etc.).

        Returns:
            SynergyResult con el resultado y metadatos de la sinergia.
        """
        validator = validator or always_valid
        opts = opts or {}
        t_start = time.perf_counter()
        attempts: list[SynergyAttempt] = []

        # ── FASE 1: Intento rápido (borrador) ────────────────────────
        draft = None
        draft_provider = None

        fast_targets = self._available_from(self.config.fast_providers)

        if not fast_targets:
            logger.info("Sin proveedores rápidos disponibles, failover directo a correctores.")
        else:
            for provider_name in fast_targets:
                if not self.config.try_all_fast_first and draft is not None:
                    break  # Solo intentar un rápido

                t0 = time.perf_counter()
                try:
                    result = await self._call_provider_async(
                        provider_name, system_prompt, user_prompt, opts
                    )
                    latency = (time.perf_counter() - t0) * 1000

                    if result and validator(result):
                        # ¡Borrador rápido validado! Retorno inmediato.
                        attempts.append(SynergyAttempt(
                            provider=provider_name, phase="draft", success=True,
                            latency_ms=latency, output_chars=len(result), validated=True,
                        ))
                        total_ms = (time.perf_counter() - t_start) * 1000
                        self._log_synergy("draft_accepted", provider_name, total_ms, True)
                        return SynergyResult(
                            value=result, provider=provider_name, phase="draft",
                            synergy_used=False, validated=True,
                            attempts=attempts, total_latency_ms=total_ms,
                        )

                    # Borrador recibido pero no validado — guardamos para corrección
                    if result:
                        draft = result
                        draft_provider = provider_name
                        attempts.append(SynergyAttempt(
                            provider=provider_name, phase="draft", success=False,
                            latency_ms=latency, output_chars=len(result),
                            validated=False, error="validation_failed",
                        ))
                        logger.info(f"Sinergia: borrador de {provider_name} no pasó validación. Activando corrección.")
                        if not self.config.try_all_fast_first:
                            break  # No intentar más rápidos
                    else:
                        attempts.append(SynergyAttempt(
                            provider=provider_name, phase="draft", success=False,
                            latency_ms=latency, error="empty_response",
                        ))

                except Exception as e:
                    latency = (time.perf_counter() - t0) * 1000
                    attempts.append(SynergyAttempt(
                        provider=provider_name, phase="draft", success=False,
                        latency_ms=latency, error=str(e)[:200],
                    ))
                    logger.warning(f"Proveedor rápido {provider_name} falló: {e}")

        # ── FASE 2: Corrección o failover ─────────────────────────────
        correction_targets = self._available_from(self.config.correction_providers)

        if not correction_targets:
            # Sin correctores disponibles
            total_ms = (time.perf_counter() - t_start) * 1000
            if draft:
                # Último recurso: devolver borrador sin validar
                self._log_synergy("draft_returned_unvalidated", draft_provider or "?", total_ms, False)
                return SynergyResult(
                    value=draft, provider=draft_provider or "unknown", phase="draft",
                    synergy_used=False, validated=False,
                    attempts=attempts, total_latency_ms=total_ms,
                )
            raise AllProvidersFailedError(attempts)

        # Construir prompt de corrección si tenemos borrador defectuoso
        # ¡SINERGIA REAL! El output defectuoso del rápido se usa como contexto
        # para que el corrector no empiece de cero, ahorrando tokens y tiempo.
        correction_count = 0
        for provider_name in correction_targets:
            if correction_count >= self.config.max_correction_attempts:
                break

            t0 = time.perf_counter()
            try:
                if draft:
                    # Sinergia: pasar borrador defectuoso como contexto
                    phase = "correction"
                    correction_system = (
                        f"{system_prompt}\n\n"
                        f"[NOTA: Un proveedor anterior ({draft_provider}) generó el siguiente output, "
                        f"pero no pasó la validación de formato/estructura. "
                        f"Úsalo como punto de partida y corrígelo para que cumpla los requisitos.]\n"
                    )
                    correction_user = (
                        f"Output previo (DEFECTUOSO, necesita corrección):\n```\n{draft}\n```\n\n"
                        f"Petición original:\n{user_prompt}"
                    )
                else:
                    # Failover: no hay borrador, ir directo
                    phase = "failover"
                    correction_system = system_prompt
                    correction_user = user_prompt

                result = await self._call_provider_async(
                    provider_name, correction_system, correction_user, opts
                )
                latency = (time.perf_counter() - t0) * 1000

                if result and validator(result):
                    # ¡Corrección exitosa!
                    synergy_used = phase == "correction"
                    attempts.append(SynergyAttempt(
                        provider=provider_name, phase=phase, success=True,
                        latency_ms=latency, output_chars=len(result), validated=True,
                    ))
                    total_ms = (time.perf_counter() - t_start) * 1000
                    self._log_synergy(phase, provider_name, total_ms, True, synergy_used=synergy_used)
                    return SynergyResult(
                        value=result, provider=provider_name, phase=phase,
                        synergy_used=synergy_used, validated=True,
                        attempts=attempts, total_latency_ms=total_ms,
                    )

                # Corrección fallida también
                attempts.append(SynergyAttempt(
                    provider=provider_name, phase=phase, success=False,
                    latency_ms=latency, output_chars=len(result) if result else 0,
                    validated=False, error="validation_failed" if result else "empty_response",
                ))
                correction_count += 1

                # Si la corrección falló pero produjo output, usarlo como nuevo borrador
                if result:
                    draft = result
                    draft_provider = provider_name

            except Exception as e:
                latency = (time.perf_counter() - t0) * 1000
                attempts.append(SynergyAttempt(
                    provider=provider_name, phase="correction" if draft else "failover",
                    success=False, latency_ms=latency, error=str(e)[:200],
                ))
                correction_count += 1

        # ── FASE 3: Último recurso ───────────────────────────────────
        total_ms = (time.perf_counter() - t_start) * 1000

        # Si tenemos algún output (aunque no validado), devolverlo con aviso
        if draft:
            self._log_synergy("unvalidated_return", draft_provider or "?", total_ms, False)
            return SynergyResult(
                value=draft, provider=draft_provider or "unknown", phase="correction",
                synergy_used=True, validated=False,
                attempts=attempts, total_latency_ms=total_ms,
            )

        raise AllProvidersFailedError(attempts)

    def execute(
        self,
        system_prompt: str,
        user_prompt: str,
        validator: Callable[[str], bool] | None = None,
        opts: dict[str, Any] | None = None,
    ) -> SynergyResult:
        """Versión síncrona de execute_async."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Ya estamos en un event loop — usar asyncio.run no funciona
            # Ejecutar de forma bloqueante en un hilo nuevo
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    self.execute_async(system_prompt, user_prompt, validator, opts)
                )
                return future.result(timeout=self.config.per_attempt_timeout_s * 4)
        else:
            return asyncio.run(self.execute_async(system_prompt, user_prompt, validator, opts))

    # ── HELPERS ────────────────────────────────────────────────────────

    def _available_from(self, provider_names: list[str]) -> list[str]:
        """Filtra proveedores que tienen key configurada y están disponibles."""
        try:
            available = self.llm._available_providers()
        except Exception:
            available = list(self.llm.providers.keys()) if hasattr(self.llm, 'providers') else []
        return [p for p in provider_names if p in available]

    async def _call_provider_async(self, provider_name: str, system_prompt: str,
                                    user_prompt: str, opts: dict) -> Optional[str]:
        """Llama a un proveedor individual vía SmartRouter."""
        try:
            # Usar SmartRouter si está disponible
            if self.llm._router:
                request = {"system_prompt": system_prompt, "user_prompt": user_prompt}
                # Forzar el proveedor si está disponible como target
                targets = self.llm._router._targets.get("default", [])
                target_ids = [t.id for t in targets]

                if provider_name in target_ids:
                    # Llamar directamente al handler del target
                    target = next(t for t in targets if t.id == provider_name)
                    try:
                        value = target.handler(request)
                        if asyncio.iscoroutine(value):
                            value = await asyncio.wait_for(
                                value, timeout=opts.get("timeout", self.config.per_attempt_timeout_s)
                            )
                        return str(value) if value else None
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"Proveedor {provider_name} excedió timeout")
                    except Exception:
                        # Si es async handler llamado desde sync, reintentar vía execute_async
                        import inspect
                        if inspect.iscoroutinefunction(target.handler):
                            result = await self.llm._router.execute_async(request, group="default")
                            return str(result.value) if result.value else None
                        raise
                else:
                    # Proveedor no registrado en router — usar fallback
                    return await self._call_provider_fallback_async(provider_name, system_prompt, user_prompt, opts)

            # Sin router — llamada directa
            return await self._call_provider_fallback_async(provider_name, system_prompt, user_prompt, opts)

        except Exception as e:
            logger.error(f"Error llamando a {provider_name}: {e}")
            raise

    async def _call_provider_fallback_async(self, provider_name: str,
                                             system_prompt: str, user_prompt: str,
                                             opts: dict) -> Optional[str]:
        """Llamada directa a un proveedor sin pasar por SmartRouter."""
        try:
            # Intentar con el método _single_provider_call de LLMClient
            if hasattr(self.llm, '_single_provider_call'):
                request = {"system_prompt": system_prompt, "user_prompt": user_prompt}
                # _single_provider_call es síncrono, ejecutar en thread pool
                loop = asyncio.get_event_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: self.llm._single_provider_call(provider_name, request)
                    ),
                    timeout=opts.get("timeout", self.config.per_attempt_timeout_s),
                )
                return result

            # Último fallback: usar call() normal forzando el proveedor
            old_preferred = getattr(self.llm, '_preferred_provider', None)
            try:
                self.llm._preferred_provider = provider_name
                loop = asyncio.get_event_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: self.llm.call(system_prompt, user_prompt)
                    ),
                    timeout=opts.get("timeout", self.config.per_attempt_timeout_s),
                )
                return result
            finally:
                self.llm._preferred_provider = old_preferred

        except asyncio.TimeoutError:
            raise TimeoutError(f"Proveedor {provider_name} excedió timeout de sinergia")
        except Exception as e:
            raise

    def _log_synergy(self, phase: str, provider: str, latency_ms: float,
                     success: bool, synergy_used: bool = False) -> None:
        """Registra un evento de sinergia para telemetría."""
        entry = {
            "timestamp": time.time(),
            "phase": phase,
            "provider": provider,
            "latency_ms": round(latency_ms, 1),
            "success": success,
            "synergy_used": synergy_used,
        }
        self._synergy_log.append(entry)
        status = "✓" if success else "✗"
        synergy_tag = " [SINERGIA]" if synergy_used else ""
        logger.info(f"Sinergia {status}: {phase} → {provider} ({latency_ms:.0f}ms){synergy_tag}")

    # ── TELEMETRÍA ────────────────────────────────────────────────────

    def get_synergy_stats(self) -> dict[str, Any]:
        """Estadísticas de sinergia para el dashboard."""
        if not self._synergy_log:
            return {"total_synergy_calls": 0}

        total = len(self._synergy_log)
        successes = sum(1 for e in self._synergy_log if e["success"])
        synergy_used = sum(1 for e in self._synergy_log if e.get("synergy_used", False))

        # By phase
        phases: dict[str, int] = {}
        for e in self._synergy_log:
            phases[e["phase"]] = phases.get(e["phase"], 0) + 1

        # By provider
        providers: dict[str, dict] = {}
        for e in self._synergy_log:
            p = e["provider"]
            if p not in providers:
                providers[p] = {"calls": 0, "successes": 0}
            providers[p]["calls"] += 1
            if e["success"]:
                providers[p]["successes"] += 1

        # Average latency
        latencies = [e["latency_ms"] for e in self._synergy_log]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0

        return {
            "total_synergy_calls": total,
            "success_rate": round(successes / total, 3) if total else 0,
            "synergy_activation_rate": round(synergy_used / total, 3) if total else 0,
            "avg_latency_ms": round(avg_latency, 1),
            "phases": phases,
            "providers": providers,
        }


__all__ = [
    "SynergyRouter",
    "SynergyConfig",
    "SynergyResult",
    "SynergyAttempt",
    "SpeedTier",
    "AllProvidersFailedError",
    "SynergyRouterError",
    # Validadores
    "is_valid_json",
    "is_valid_json_with_fields",
    "is_non_empty",
    "always_valid",
]
