"""
smart_router.py — Portal Pi
===========================
Adaptación del SmartRouter genérico para enrutar llamadas LLM con resiliencia.

Cada proveedor LLM (groq, nvidia, gemini_flash, etc.) se registra como un Target.
El SmartRouter:
  - Puntúa destinos por peso × latencia × tasa de éxito × disponibilidad (circuit breaker)
  - Reintenta automáticamente con backoff + jitter
  - Abre circuitos cuando un proveedor falla consistentemente
  - Degradación controlada con fallback handler
  - Telemetría en memoria con ventana deslizante
  - Alertas configurables (error_rate, latency, circuit_open)

Reemplaza el fallback secuencial simple de llm_client.py por un sistema robusto.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Optional

logger = logging.getLogger("smart_router")


# ─── EXCEPCIONES ──────────────────────────────────────────────────────────

class SmartRouterError(Exception):
    """Error base del módulo."""


class NoTargetsAvailableError(SmartRouterError):
    """No hay destinos registrados en el grupo solicitado."""


class AllTargetsDownError(SmartRouterError):
    """Todos los destinos del grupo han fallado y no hay fallback_handler."""

    def __init__(self, group: str, attempts: list["AttemptRecord"]):
        self.group = group
        self.attempts = attempts
        summary = ", ".join(f"{a.target_id}: {a.error!r}" for a in attempts)
        super().__init__(f"Todos los destinos del grupo '{group}' han fallado. Intentos: {summary}")


# ─── DATA CLASSES ─────────────────────────────────────────────────────────

@dataclass
class Target:
    id: str
    handler: Callable[..., Any]          # función sync o async; el router lo detecta
    weight: float = 1.0
    group: str = "default"


@dataclass
class AttemptRecord:
    target_id: str
    error: Optional[BaseException]
    latency_ms: float


@dataclass
class RouteResult:
    value: Any
    target_id: str
    degraded: bool = False               # True si vino del fallback_handler
    forced_fallback: bool = False        # True si se usó un destino con score 0 como último recurso
    attempts: list[AttemptRecord] = field(default_factory=list)


# ─── CIRCUIT BREAKER ──────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    failure_threshold: float = 0.5       # tasa de error en la ventana para abrir
    min_requests: int = 10               # muestras mínimas antes de evaluar
    window_s: int = 30
    open_duration_s: int = 20
    half_open_max_calls: int = 3


class CircuitBreaker:
    """Circuit breaker por destino. Thread-safe."""

    def __init__(
        self,
        config: CircuitBreakerConfig | None = None,
        on_state_change: Optional[Callable[[str, CircuitState, CircuitState], None]] = None,
    ):
        self.config = config or CircuitBreakerConfig()
        self._on_state_change = on_state_change
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._opened_at = 0.0
        self._events: Deque[tuple[float, bool]] = deque()
        self._half_open_in_flight = 0
        self._half_open_successes = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_transition_locked()
            return self._state

    def allow_request(self) -> bool:
        with self._lock:
            self._maybe_transition_locked()
            if self._state is CircuitState.CLOSED:
                return True
            if self._state is CircuitState.OPEN:
                return False
            if self._half_open_in_flight < self.config.half_open_max_calls:
                self._half_open_in_flight += 1
                return True
            return False

    def on_success(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._half_open_successes += 1
                if self._half_open_successes >= self.config.half_open_max_calls:
                    self._transition_locked(CircuitState.CLOSED)
            else:
                self._record_locked(True)

    def on_failure(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._transition_locked(CircuitState.OPEN)
                return
            self._record_locked(False)
            total = len(self._events)
            if total >= self.config.min_requests:
                errors = sum(1 for _, ok in self._events if not ok)
                if errors / total >= self.config.failure_threshold:
                    self._transition_locked(CircuitState.OPEN)

    def availability_factor(self) -> float:
        s = self.state
        return {CircuitState.CLOSED: 1.0, CircuitState.HALF_OPEN: 0.5, CircuitState.OPEN: 0.0}[s]

    def _record_locked(self, success: bool) -> None:
        now = time.monotonic()
        self._events.append((now, success))
        cutoff = now - self.config.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _maybe_transition_locked(self) -> None:
        if self._state is CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self.config.open_duration_s:
                self._transition_locked(CircuitState.HALF_OPEN)

    def _transition_locked(self, new_state: CircuitState) -> None:
        old = self._state
        if old is new_state:
            return
        self._state = new_state
        if new_state is CircuitState.OPEN:
            self._opened_at = time.monotonic()
        if new_state is CircuitState.HALF_OPEN:
            self._half_open_in_flight = 0
            self._half_open_successes = 0
        if new_state is CircuitState.CLOSED:
            self._events.clear()
        logger.info("CircuitBreaker %s -> %s", old.value, new_state.value)
        if self._on_state_change:
            try:
                self._on_state_change("?", old, new_state)
            except Exception:
                logger.exception("Error en callback on_state_change")


# ─── TELEMETRÍA ───────────────────────────────────────────────────────────

@dataclass
class WindowStats:
    target_id: str
    window_s: int
    requests: int = 0
    errors: int = 0
    error_rate: float = 0.0
    latency_avg_ms: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    throughput_rps: float = 0.0
    in_flight: int = 0
    fallback_events: int = 0
    circuit_state: str = CircuitState.CLOSED.value
    has_data: bool = False


@dataclass
class TelemetryConfig:
    window_s: int = 60
    aggregation_interval_s: float = 5.0
    autostart_aggregator: bool = True


class TelemetryCollector:
    """Recolección in-process no bloqueante."""

    def __init__(self, config: TelemetryConfig | None = None):
        self.config = config or TelemetryConfig()
        self._lock = threading.Lock()
        self._events: dict[str, Deque[tuple[float, bool, float]]] = {}
        self._in_flight: dict[str, int] = {}
        self._fallback_events: dict[str, int] = {}
        self._circuit_state: dict[str, str] = {}
        self._stats: dict[str, WindowStats] = {}
        self._aggregator_stop = threading.Event()
        self._aggregator_thread: Optional[threading.Thread] = None
        self._on_aggregate: list[Callable[[dict[str, WindowStats]], None]] = []
        if self.config.autostart_aggregator:
            self.start_aggregator()

    def record(self, target_id: str, success: bool, latency_ms: float) -> None:
        with self._lock:
            self._events.setdefault(target_id, deque()).append((time.monotonic(), success, latency_ms))

    def incr_in_flight(self, target_id: str, delta: int) -> None:
        with self._lock:
            self._in_flight[target_id] = max(0, self._in_flight.get(target_id, 0) + delta)

    def record_fallback_event(self, target_id: str) -> None:
        with self._lock:
            self._fallback_events[target_id] = self._fallback_events.get(target_id, 0) + 1

    def set_circuit_state(self, target_id: str, state: CircuitState) -> None:
        with self._lock:
            self._circuit_state[target_id] = state.value

    def add_aggregate_listener(self, cb: Callable[[dict[str, WindowStats]], None]) -> None:
        self._on_aggregate.append(cb)

    def start_aggregator(self) -> None:
        if self._aggregator_thread and self._aggregator_thread.is_alive():
            return
        self._aggregator_stop.clear()
        self._aggregator_thread = threading.Thread(
            target=self._aggregate_loop, name="smart-router-telemetry", daemon=True
        )
        self._aggregator_thread.start()

    def stop_aggregator(self) -> None:
        self._aggregator_stop.set()
        if self._aggregator_thread:
            self._aggregator_thread.join(timeout=2.0)

    def _aggregate_loop(self) -> None:
        while not self._aggregator_stop.wait(self.config.aggregation_interval_s):
            self.aggregate_once()

    def aggregate_once(self) -> None:
        now = time.monotonic()
        cutoff = now - self.config.window_s
        with self._lock:
            snapshot_events = {tid: list(evts) for tid, evts in self._events.items()}
            in_flight = dict(self._in_flight)
            fallbacks = dict(self._fallback_events)
            circuits = dict(self._circuit_state)

        stats: dict[str, WindowStats] = {}
        for tid, evts in snapshot_events.items():
            recent = [(ts, ok, ms) for ts, ok, ms in evts if ts >= cutoff]
            ws = WindowStats(
                target_id=tid,
                window_s=self.config.window_s,
                in_flight=in_flight.get(tid, 0),
                fallback_events=fallbacks.get(tid, 0),
                circuit_state=circuits.get(tid, CircuitState.CLOSED.value),
            )
            if recent:
                lat = sorted(ms for _, _, ms in recent)
                ws.has_data = True
                ws.requests = len(recent)
                ws.errors = sum(1 for _, ok, _ in recent if not ok)
                ws.error_rate = ws.errors / ws.requests
                ws.latency_avg_ms = statistics.fmean(lat)
                ws.latency_p50_ms = _percentile(lat, 0.50)
                ws.latency_p95_ms = _percentile(lat, 0.95)
                ws.latency_p99_ms = _percentile(lat, 0.99)
                ws.throughput_rps = ws.requests / self.config.window_s
            stats[tid] = ws

        with self._lock:
            self._stats = stats
        for cb in self._on_aggregate:
            try:
                cb(stats)
            except Exception:
                logger.exception("Error en listener de agregación")

    def get_stats(self, target_id: str) -> WindowStats:
        with self._lock:
            return self._stats.get(target_id) or WindowStats(target_id=target_id, window_s=self.config.window_s)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {tid: vars(ws).copy() for tid, ws in self._stats.items()}


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


# ─── ALERTAS ──────────────────────────────────────────────────────────────

@dataclass
class AlertRule:
    metric: str
    threshold: float
    window_s: int = 60
    on_trigger: Optional[Callable[[str, float], None]] = None


class AlertManager:
    def __init__(self, rules: list[AlertRule] | None = None, telemetry: TelemetryCollector | None = None):
        self.rules = rules or []
        if telemetry:
            telemetry.add_aggregate_listener(self.evaluate)

    def add_rule(self, rule: AlertRule) -> None:
        self.rules.append(rule)

    def evaluate(self, stats: dict[str, WindowStats]) -> None:
        for rule in self.rules:
            for tid, ws in stats.items():
                if rule.metric == "circuit_open":
                    triggered = ws.circuit_state == CircuitState.OPEN.value
                    value = 1.0 if triggered else 0.0
                else:
                    value = float(getattr(ws, rule.metric, 0.0))
                    triggered = ws.has_data and value >= rule.threshold
                if triggered:
                    self._fire(rule, tid, value)

    def _fire(self, rule: AlertRule, target_id: str, value: float) -> None:
        logger.warning("ALERTA %s=%.3f (umbral %.3f) en %s", rule.metric, value, rule.threshold, target_id)
        if rule.on_trigger:
            try:
                rule.on_trigger(target_id, value)
            except Exception:
                logger.exception("Error en callback de alerta")


# ─── RETRY / FALLBACK ─────────────────────────────────────────────────────

@dataclass
class RetryConfig:
    max_retries: int = 2
    base_delay_s: float = 0.05
    max_delay_s: float = 2.0
    jitter: float = 0.25

    def delay_for(self, attempt: int) -> float:
        delay = min(self.base_delay_s * (2 ** attempt), self.max_delay_s)
        return delay * (1 + random.uniform(-self.jitter, self.jitter))


# ─── ROUTING ──────────────────────────────────────────────────────────────

@dataclass
class RoutingDecision:
    target_id: str
    score: float
    reason: str


@dataclass
class RouterConfig:
    new_target_neutral_score: float = 0.5
    latency_reference_ms: float = 500.0
    retry: RetryConfig = field(default_factory=RetryConfig)


class SmartRouter:
    """
    Router inteligente para llamadas LLM con circuit breaker, telemetría y fallback.
    
    Cada proveedor LLM se registra como un Target. El router:
    - Puntúa destinos por peso × latencia × tasa de éxito × disponibilidad
    - Reintenta automáticamente con backoff + jitter
    - Abre circuitos cuando un proveedor falla consistentemente
    - Degradación controlada con fallback handler
    - Telemetría en memoria con ventana deslizante
    """

    def __init__(
        self,
        config: RouterConfig | None = None,
        telemetry: TelemetryCollector | None = None,
        cb_config: CircuitBreakerConfig | None = None,
        fallback_handlers: dict[str, Callable[..., Any]] | None = None,
        on_fallback_event: Optional[Callable[[dict], None]] = None,
    ):
        self.config = config or RouterConfig()
        self.telemetry = telemetry or TelemetryCollector()
        self.cb_config = cb_config or CircuitBreakerConfig()
        self._targets: dict[str, list[Target]] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._fallback_handlers = fallback_handlers or {}
        self._on_fallback_event = on_fallback_event
        self._lock = threading.Lock()
        self._rr_counter: dict[str, int] = {}
        self.alerts = AlertManager(telemetry=self.telemetry)

    # -- registro ------------------------------------------------------------

    def register_target(self, target: Target) -> None:
        if target.weight <= 0:
            raise ValueError(f"weight debe ser > 0 (target {target.id})")
        with self._lock:
            group = self._targets.setdefault(target.group, [])
            if any(t.id == target.id for t in group):
                raise ValueError(f"Target duplicado: {target.id}")
            group.append(target)
            self._breakers[target.id] = CircuitBreaker(
                self.cb_config,
                on_state_change=lambda _tid, old, new, tid=target.id: self._cb_state_changed(tid, old, new),
            )

    def set_fallback_handler(self, group: str, handler: Callable[..., Any]) -> None:
        self._fallback_handlers[group] = handler

    def _cb_state_changed(self, target_id: str, old: CircuitState, new: CircuitState) -> None:
        self.telemetry.set_circuit_state(target_id, new)
        self._emit_fallback_event(target_id=target_id, reason="circuit_state_change",
                                  new_state=new.value)

    def _emit_fallback_event(self, **event: Any) -> None:
        event.setdefault("timestamp", time.time())
        logger.info("fallback_event: %s", event)
        if self._on_fallback_event:
            try:
                self._on_fallback_event(event)
            except Exception:
                logger.exception("Error en on_fallback_event")

    # -- scoring --------------------------------------------------------------

    def _score(self, target: Target) -> tuple[float, str]:
        breaker = self._breakers[target.id]
        availability = breaker.availability_factor()
        if availability == 0.0:
            return 0.0, "scoring"
        stats = self.telemetry.get_stats(target.id)
        if not stats.has_data:
            return target.weight * self.config.new_target_neutral_score * availability, "scoring"
        norm_latency = stats.latency_avg_ms / self.config.latency_reference_ms
        score = (
            target.weight
            * (1.0 / (1.0 + norm_latency))
            * (1.0 - stats.error_rate)
            * availability
        )
        return score, "scoring"

    def _rank(self, group: str) -> list[tuple[Target, float, str]]:
        targets = self._targets.get(group, [])
        if not targets:
            raise NoTargetsAvailableError(f"Grupo '{group}' sin destinos registrados")
        scored = [(t, *self._score(t)) for t in targets]
        scored.sort(key=lambda x: x[1], reverse=True)

        if scored and all(s == scored[0][1] for s, *_ in [(x[1],) for x in scored]):
            best_score = scored[0][1]
            tied = [x for x in scored if x[1] == best_score]
            if len(tied) > 1:
                n = self._rr_counter.get(group, 0)
                self._rr_counter[group] = n + 1
                rot = tied[n % len(tied):] + tied[:n % len(tied)]
                rest = [x for x in scored if x[1] != best_score]
                scored = [(t, s, "round_robin_fallback") for t, s, _ in rot] + rest
        return scored

    def route(self, request: Any = None, group: str = "default") -> RoutingDecision:
        ranked = self._rank(group)
        best_t, best_score, reason = ranked[0]
        if best_score == 0.0:
            reason = "forced_fallback"
        logger.info('routing_decision group=%s target=%s score=%.4f reason="%s"',
                    group, best_t.id, best_score, reason)
        return RoutingDecision(target_id=best_t.id, score=best_score, reason=reason)

    async def route_async(self, request: Any = None, group: str = "default") -> RoutingDecision:
        return self.route(request, group)

    # -- ejecución sync ------------------------------------------------------

    def execute(self, request: Any = None, group: str = "default", *args, **kwargs) -> RouteResult:
        ranked = self._rank(group)
        attempts: list[AttemptRecord] = []
        forced = ranked[0][1] == 0.0

        for target, score, reason in ranked:
            breaker = self._breakers[target.id]
            if not breaker.allow_request():
                continue
            ok, result = self._call_with_retries(target, request, attempts, *args, **kwargs)
            if ok:
                return RouteResult(value=result, target_id=target.id,
                                   forced_fallback=forced or reason == "forced_fallback",
                                   attempts=attempts)
            self.telemetry.record_fallback_event(target.id)
            self._emit_fallback_event(target_id=target.id, reason="target_exhausted",
                                      new_state="rerouting")

        fh = self._fallback_handlers.get(group)
        if fh:
            self._emit_fallback_event(target_id="*", reason="total_fallback", new_state="degraded")
            return RouteResult(value=fh(request, *args, **kwargs), target_id="__fallback__",
                               degraded=True, attempts=attempts)
        raise AllTargetsDownError(group, attempts)

    def _call_with_retries(self, target: Target, request: Any,
                           attempts: list[AttemptRecord], *args, **kwargs) -> tuple[bool, Any]:
        breaker = self._breakers[target.id]
        retry = self.config.retry
        for attempt in range(retry.max_retries + 1):
            t0 = time.perf_counter()
            self.telemetry.incr_in_flight(target.id, +1)
            try:
                value = target.handler(request, *args, **kwargs)
                if inspect.isawaitable(value):
                    raise TypeError(
                        f"Handler async '{target.id}' invocado desde execute(); usa execute_async()"
                    )
                latency = (time.perf_counter() - t0) * 1000
                breaker.on_success()
                self.telemetry.record(target.id, True, latency)
                attempts.append(AttemptRecord(target.id, None, latency))
                return True, value
            except Exception as exc:
                latency = (time.perf_counter() - t0) * 1000
                breaker.on_failure()
                self.telemetry.record(target.id, False, latency)
                attempts.append(AttemptRecord(target.id, exc, latency))
                if attempt < retry.max_retries:
                    time.sleep(retry.delay_for(attempt))
            finally:
                self.telemetry.incr_in_flight(target.id, -1)
        return False, None

    # -- ejecución async -----------------------------------------------------

    async def execute_async(self, request: Any = None, group: str = "default", *args, **kwargs) -> RouteResult:
        ranked = self._rank(group)
        attempts: list[AttemptRecord] = []
        forced = ranked[0][1] == 0.0

        for target, score, reason in ranked:
            breaker = self._breakers[target.id]
            if not breaker.allow_request():
                continue
            ok, result = await self._call_with_retries_async(target, request, attempts, *args, **kwargs)
            if ok:
                return RouteResult(value=result, target_id=target.id,
                                   forced_fallback=forced or reason == "forced_fallback",
                                   attempts=attempts)
            self.telemetry.record_fallback_event(target.id)
            self._emit_fallback_event(target_id=target.id, reason="target_exhausted",
                                      new_state="rerouting")

        fh = self._fallback_handlers.get(group)
        if fh:
            self._emit_fallback_event(target_id="*", reason="total_fallback", new_state="degraded")
            value = fh(request, *args, **kwargs)
            if inspect.isawaitable(value):
                value = await value
            return RouteResult(value=value, target_id="__fallback__", degraded=True, attempts=attempts)
        raise AllTargetsDownError(group, attempts)

    async def _call_with_retries_async(self, target: Target, request: Any,
                                       attempts: list[AttemptRecord], *args, **kwargs) -> tuple[bool, Any]:
        breaker = self._breakers[target.id]
        retry = self.config.retry
        for attempt in range(retry.max_retries + 1):
            t0 = time.perf_counter()
            self.telemetry.incr_in_flight(target.id, +1)
            try:
                value = target.handler(request, *args, **kwargs)
                if inspect.isawaitable(value):
                    value = await value
                latency = (time.perf_counter() - t0) * 1000
                breaker.on_success()
                self.telemetry.record(target.id, True, latency)
                attempts.append(AttemptRecord(target.id, None, latency))
                return True, value
            except Exception as exc:
                latency = (time.perf_counter() - t0) * 1000
                breaker.on_failure()
                self.telemetry.record(target.id, False, latency)
                attempts.append(AttemptRecord(target.id, exc, latency))
                if attempt < retry.max_retries:
                    await asyncio.sleep(retry.delay_for(attempt))
            finally:
                self.telemetry.incr_in_flight(target.id, -1)
        return False, None

    # -- decorador -----------------------------------------------------------

    def managed_call(self, group: str = "default"):
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            with self._lock:
                existing = self._targets.get(group, [])
            if existing:
                self._fallback_handlers[group] = fn
                return fn
            self.register_target(Target(id=fn.__name__, handler=fn, group=group))
            is_async = inspect.iscoroutinefunction(fn)
            if is_async:
                async def awrapper(*args, **kwargs):
                    req = args[0] if args else None
                    return (await self.execute_async(req, group)).value
                return awrapper
            def wrapper(*args, **kwargs):
                req = args[0] if args else None
                return self.execute(req, group).value
            return wrapper
        return decorator

    # -- snapshot para dashboard --------------------------------------------

    def get_routing_status(self) -> dict[str, Any]:
        """Devuelve el estado completo del router para el dashboard."""
        telemetry_snapshot = self.telemetry.snapshot()
        breakers = {}
        for tid, cb in self._breakers.items():
            breakers[tid] = {
                "state": cb.state.value,
                "availability": cb.availability_factor(),
            }
        targets_info = {}
        for group, tlist in self._targets.items():
            for t in tlist:
                score, reason = self._score(t)
                targets_info[t.id] = {
                    "group": group,
                    "weight": t.weight,
                    "score": round(score, 4),
                    "circuit_state": self._breakers[t.id].state.value,
                }
        return {
            "targets": targets_info,
            "breakers": breakers,
            "telemetry": telemetry_snapshot,
        }


__all__ = [
    "SmartRouter", "Target", "RoutingDecision", "RouteResult", "AttemptRecord",
    "RouterConfig", "RetryConfig",
    "CircuitBreaker", "CircuitBreakerConfig", "CircuitState",
    "TelemetryCollector", "TelemetryConfig", "WindowStats",
    "AlertManager", "AlertRule",
    "SmartRouterError", "NoTargetsAvailableError", "AllTargetsDownError",
]
