"""
llm_client.py
Cliente LLM multi-provider para Portal Pi.
API keys en config/.credentials.json (cifrado con Fernet, nunca se comparte).

Usa SmartRouter para:
- Puntuar proveedores por peso × latencia × tasa de éxito × disponibilidad
- Circuit breaker: aislar proveedores que fallan consistentemente
- Reintentar con backoff + jitter antes de pasar al siguiente
- Degradación controlada con fallback handler
- Telemetría en memoria con ventana deslizante
"""

import json
import asyncio
import base64
from pathlib import Path
from typing import Callable, Dict, Any, Optional, List, AsyncGenerator
from datetime import datetime, timezone

from openai import OpenAI
import websockets

from scripts.smart_router import (
    SmartRouter, Target, RouterConfig, RetryConfig,
    CircuitBreakerConfig, TelemetryConfig, TelemetryCollector,
    AllTargetsDownError, RouteResult,
)

from scripts.synergy_router import (
    SynergyRouter, SynergyConfig, SynergyResult, SynergyAttempt,
    is_valid_json as synergy_is_valid_json,
    is_non_empty, always_valid, AllProvidersFailedError as SynergyAllProvidersFailedError,
)


from scripts.paths import BASE_DIR, LLM_CONFIG_PATH, CREDENTIALS_PATH, CRED_KEY_PATH, LOGS_DIR, LLM_LOG, log_to_file

CONFIG_PATH = LLM_CONFIG_PATH


# ─── Cifrado de credenciales con Fernet ────────────────────────────────
# La clave Fernet se autogenera en config/.cred_key la primera vez.
# Si no existe cryptography, las credenciales se guardan en texto plano
# (mantiene compatibilidad con instalaciones mínimas).

def _get_fernet():
    """Retorna una instancia Fernet para cifrar/descifrar credenciales."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    key_path = CRED_KEY_PATH
    if not key_path.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_path.write_bytes(key)
    else:
        key = key_path.read_bytes()
    try:
        return Fernet(key)
    except Exception:
        # Clave corrupta → regenerar (las credenciales cifradas previas se perderán)
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        return Fernet(key)


class LLMClientError(Exception):
    pass


class ProviderResult:
    def __init__(self, name: str, status: str, detail: str = "", model: str = ""):
        self.name = name
        self.status = status
        self.detail = detail
        self.model = model

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "model": self.model}


class LLMClient:
    """
    Cliente LLM multi-provider con fallback en CADA llamada.
    
    - config/llm.json → modelos, URLs, temperaturas (sin secrets)
    - config/.credentials.json → API keys (nunca se comparte)
    
    Si el proveedor activo falla (429, timeout, error), prueba el siguiente
    automáticamente en la misma llamada. El usuario nunca tiene que preocuparse
    de cuál se usa.
    """

    def __init__(self, config_path: Optional[str] = None, credentials_path: Optional[str] = None) -> None:
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self.credentials_path = Path(credentials_path) if credentials_path else CREDENTIALS_PATH
        self._preferred_provider: Optional[str] = None  # Último que funcionó
        self._call_log: List[Dict[str, Any]] = []  # Últimas llamadas
        self._router: Optional[SmartRouter] = None  # Router inteligente (se inicializa en _load_config)
        self._synergy: Optional[SynergyRouter] = None  # Router sinérgico (se inicializa en _load_config)
        self._user_credentials: Dict[str, str] = {}  # Keys inyectadas desde Supabase (por request)
        self._load_config()

    def _load_config(self) -> None:
        if not self.config_path.exists():
            raise LLMClientError(f"No se encontró {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            raw = f.read()
        lines = [l for l in raw.splitlines() if not l.strip().startswith("//")]
        self.config = json.loads("\n".join(lines))
        self.providers = self.config.get("providers", {})
        self.fallback_order = self.config.get("fallback_order", list(self.providers.keys()))
        # ── Inicializar SmartRouter con los proveedores configurados ──
        self._init_router()

    def _init_router(self) -> None:
        """Registra cada proveedor con key como Target en el SmartRouter."""
        creds = self._load_credentials()
        # Configuración del router: pesos por rol, circuit breaker adaptado a LLMs
        roles_config = self.config.get("roles", {})
        cb_config = CircuitBreakerConfig(
            failure_threshold=0.6,   # LLMs pueden fallar transitoriamente (429, timeouts)
            min_requests=5,          # Pocas muestras para empezar a decidir
            window_s=60,             # Ventana de 1 minuto
            open_duration_s=30,      # Reintentar tras 30s en vez de 20s
            half_open_max_calls=2,   # 2 llamadas de prueba en half-open
        )
        telemetry_config = TelemetryConfig(
            window_s=120,            # Ventana de 2 minutos para estadísticas
            aggregation_interval_s=5.0,
        )
        router_config = RouterConfig(
            new_target_neutral_score=0.5,
            latency_reference_ms=1000.0,  # Referencia: 1s para LLMs
            retry=RetryConfig(max_retries=1, base_delay_s=0.1, max_delay_s=3.0, jitter=0.3),
        )

        self._router = SmartRouter(
            config=router_config,
            telemetry=TelemetryCollector(telemetry_config),
            cb_config=cb_config,
            fallback_handlers={"default": self._fallback_handler},
            on_fallback_event=self._on_router_fallback_event,
        )

        # Registrar cada proveedor con key como Target
        for name in self.fallback_order:
            pcfg = self.providers.get(name, {})
            if not pcfg or not creds.get(name, ""):
                continue  # Sin key → no registrar
            # Peso basado en el rol preferido del proveedor
            weight = 1.0
            # Buscar en roles si tiene preferencia
            for role_name, role_cfg in roles_config.items():
                if name in role_cfg.get("preferred", []):
                    needs = role_cfg.get("needs", [])
                    if "speed" in needs:
                        weight = 1.5  # Más peso a rápidos
                    elif "reasoning" in needs:
                        weight = 1.2  # Más peso a razonamiento
                    elif "writing" in needs:
                        weight = 1.1
                    break
            target = Target(
                id=name,
                handler=lambda req, _name=name: self._single_provider_call(_name, req),
                weight=weight,
                group="default",
            )
            try:
                self._router.register_target(target)
            except ValueError:
                pass  # Ya registrado (puede pasar en reload)

        # ── Inicializar SynergyRouter ──
        synergy_config = SynergyConfig(
            fast_providers=[name for name in self.fallback_order
                            if name in {"groq", "cerebras"}],
            correction_providers=[name for name in self.fallback_order
                                  if name not in {"groq", "cerebras"}],
            per_attempt_timeout_s=float(self.config.get("synergy", {}).get("timeout", 15.0)),
            max_correction_attempts=int(self.config.get("synergy", {}).get("max_correction_attempts", 2)),
            try_all_fast_first=bool(self.config.get("synergy", {}).get("try_all_fast_first", True)),
        )
        self._synergy = SynergyRouter(self, synergy_config)

    def _single_provider_call(self, provider_name: str, request: Any) -> str:
        """Handler para el SmartRouter: llama a un solo proveedor.
        request es un dict con {system_prompt, user_prompt}."""
        client, pcfg = self._make_client(provider_name)
        model = pcfg.get("model", "")
        system_prompt = request.get("system_prompt", "") if isinstance(request, dict) else ""
        user_prompt = request.get("user_prompt", "") if isinstance(request, dict) else str(request)
        self._log(f"router_call: {provider_name} / {model} ({len(system_prompt)}c/{len(user_prompt)}c)")

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=pcfg.get("max_tokens", 2048),
            temperature=pcfg.get("temperature", 0.3),
            timeout=pcfg.get("timeout", 60),
        )
        text = response.choices[0].message.content.strip()
        self._preferred_provider = provider_name
        self._log(f"router_ok: {provider_name} respondió {len(text)} chars")
        return text

    def _fallback_handler(self, request: Any, *args, **kwargs) -> str:
        """Fallback de degradación controlada: devuelve un mensaje de error en vez de crashear."""
        self._log("fallback_handler: todos los proveedores caídos, devolviendo respuesta degradada", "WARN")
        return "Lo siento, en este momento todos los proveedores de IA están experimentando problemas. Por favor, inténtalo de nuevo en unos segundos."

    def _on_router_fallback_event(self, event: dict) -> None:
        """Callback cuando el SmartRouter ejecuta un fallback."""
        self._log(f"router_fallback: {event}", "WARN")

    def _load_credentials(self) -> Dict[str, str]:
        if not self.credentials_path.exists():
            self._save_credentials({})
            return {}
        with open(self.credentials_path, "r", encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)

        # Auto-migración: texto plano → cifrado
        if not data.get("_encrypted"):
            providers = data.get("providers", {})
            self._save_credentials(providers)
            return providers

        # Descifrar valores
        fernet = _get_fernet()
        encrypted_providers = data.get("providers", {})
        if not fernet:
            # Sin cryptography → las claves ya están cifradas y no se pueden leer
            return {}
        result = {}
        for name, enc_value in encrypted_providers.items():
            try:
                result[name] = fernet.decrypt(enc_value.encode()).decode()
            except Exception:
                pass  # Clave corrupta o ilegible → ignorar
        return result

    def _save_credentials(self, creds: Dict[str, str]) -> None:
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        fernet = _get_fernet()
        if fernet:
            encrypted_providers = {
                name: fernet.encrypt(key.encode()).decode()
                for name, key in creds.items()
            }
        else:
            encrypted_providers = creds
        with open(self.credentials_path, "w", encoding="utf-8") as f:
            json.dump({
                "comment": "Credenciales cifradas con Fernet. La clave de descifrado está en config/.cred_key",
                "_encrypted": bool(fernet),
                "providers": encrypted_providers
            }, f, indent=2, ensure_ascii=False)

    def get_credential(self, provider_name: str) -> str:
        # Prioridad: keys inyectadas desde Supabase > archivo local cifrado
        if provider_name in self._user_credentials:
            return self._user_credentials[provider_name]
        creds = self._load_credentials()
        return creds.get(provider_name, "")

    def set_user_credentials(self, user_creds: Dict[str, str]) -> None:
        """Inyecta credenciales de usuario desde Supabase. Prioridad sobre el archivo local."""
        self._user_credentials = user_creds
        # Re-registrar targets del router con las nuevas keys
        self._init_router()

    def set_credential(self, provider_name: str, api_key: str) -> None:
        creds = self._load_credentials()
        creds[provider_name] = api_key
        self._save_credentials(creds)
        if provider_name == self._preferred_provider:
            self._preferred_provider = None

    def _log(self, message: str, level: str = "INFO") -> None:
        log_to_file(LLM_LOG, message, level)

    def _make_client(self, provider_name: str) -> tuple:
        if provider_name not in self.providers:
            raise LLMClientError(f"Proveedor '{provider_name}' no encontrado en config")
        pcfg = self.providers[provider_name]
        api_key = self.get_credential(provider_name)
        if not api_key:
            raise LLMClientError(f"API key no configurada para '{provider_name}'")
        client = OpenAI(
            api_key=api_key,
            base_url=pcfg.get("base_url", ""),
            default_headers=pcfg.get("extra_headers", {}) or None,
        )
        return client, pcfg

    def _available_providers(self) -> List[str]:
        """Lista de proveedores con key configurada, en orden de preferencia."""
        creds = self._load_credentials()
        available = [name for name in self.fallback_order
                     if name in self.providers and creds.get(name, "")]
        # Si tenemos un preferred que funcionó antes, ponerlo primero
        if self._preferred_provider and self._preferred_provider in available:
            available.remove(self._preferred_provider)
            available.insert(0, self._preferred_provider)
        return available

    def call(self, system_prompt: str, user_prompt: str) -> str:
        """
        Llamada al LLM vía SmartRouter.
        El router elige el mejor proveedor basándose en:
        - Peso del proveedor (por rol)
        - Latencia media reciente
        - Tasa de éxito reciente
        - Estado del circuit breaker (abierto/cerrado/semi-abierto)
        
        Si un proveedor falla, reintenta con backoff + jitter.
        Si todos fallan, ejecuta el fallback_handler (degradación controlada).
        """
        available = self._available_providers()
        if not available:
            raise LLMClientError(
                "No hay proveedores con API key configurada. "
                "Introduce al menos una desde el dashboard (tab Pipeline)."
            )

        # ── Usar SmartRouter si está disponible ──
        if self._router:
            request = {"system_prompt": system_prompt, "user_prompt": user_prompt}
            try:
                result = self._router.execute(request, group="default")
                if result.degraded:
                    self._log(f"router: respuesta degradada ({result.target_id})", "WARN")
                # Log de la llamada
                self._call_log.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "provider": result.target_id,
                    "model": self.providers.get(result.target_id, {}).get("model", ""),
                    "input_chars": len(system_prompt) + len(user_prompt),
                    "output_chars": len(result.value) if isinstance(result.value, str) else 0,
                    "status": "ok" if not result.degraded else "degraded",
                    "forced_fallback": result.forced_fallback,
                    "attempts": len(result.attempts),
                })
                return result.value
            except AllTargetsDownError as exc:
                self._log(f"router: todos los destinos caídos: {exc}", "ERROR")
                raise LLMClientError(str(exc))

        # ── Fallback al modo secuencial simple (sin router) ──
        errors = []
        for name in available:
            try:
                client, pcfg = self._make_client(name)
                model = pcfg.get("model", "")
                self._log(f"call_fallback: {name} / {model} ({len(system_prompt)}c/{len(user_prompt)}c)")
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=pcfg.get("max_tokens", 2048),
                    temperature=pcfg.get("temperature", 0.3),
                    timeout=pcfg.get("timeout", 60),
                )
                text = response.choices[0].message.content.strip()
                self._preferred_provider = name
                self._log(f"ok: {name} respondió {len(text)} chars")
                self._call_log.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "provider": name,
                    "model": model,
                    "input_chars": len(system_prompt) + len(user_prompt),
                    "output_chars": len(text),
                    "status": "ok",
                })
                return text
            except Exception as exc:
                err_msg = str(exc)[:200]
                self._log(f"fail: {name} - {err_msg}", "WARN")
                errors.append(f"{name}: {err_msg}")
                self._call_log.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "provider": name,
                    "model": self.providers.get(name, {}).get("model", ""),
                    "status": "error",
                    "error": err_msg,
                })
                continue

        raise LLMClientError(
            f"Todos los proveedores fallaron:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    def call_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        raw = self.call(system_prompt, user_prompt)
        return parse_json_response(raw)

    def test_all(self) -> List[ProviderResult]:
        results = []
        for name in self.fallback_order:
            if name not in self.providers:
                results.append(ProviderResult(name, "skipped", "No en config"))
                continue
            pcfg = self.providers[name]
            api_key = self.get_credential(name)
            if not api_key:
                results.append(ProviderResult(name, "no_key", "API key no introducida", pcfg.get("model", "")))
                continue
            try:
                client, _ = self._make_client(name)
                response = client.chat.completions.create(
                    model=pcfg.get("model", ""),
                    messages=[{"role": "user", "content": "Responde solo: OK"}],
                    max_tokens=10,
                    temperature=0,
                    timeout=15,
                )
                text = response.choices[0].message.content.strip()
                results.append(ProviderResult(name, "ok", f"Respondió: {text}", pcfg.get("model", "")))
                if not self._preferred_provider:
                    self._preferred_provider = name
            except Exception as exc:
                results.append(ProviderResult(name, "error", str(exc)[:120], pcfg.get("model", "")))

        # Actualizar preferred al primero que funcione
        for r in results:
            if r.status == "ok":
                self._preferred_provider = r.name
                break

        return results

    def get_last_call_info(self) -> Optional[Dict[str, Any]]:
        """Info de la última llamada al LLM."""
        if self._call_log:
            last = self._call_log[-1]
            return {
                "provider": last.get("provider"),
                "model": last.get("model"),
                "status": last.get("status"),
                "output_chars": last.get("output_chars", 0),
                "error": last.get("error"),
            }
        return None

    def get_config_info(self) -> Dict[str, Any]:
        creds = self._load_credentials()
        providers_info = {}
        for name, pcfg in self.providers.items():
            key = creds.get(name, "")
            masked = key[:6] + "..." + key[-4:] if len(key) > 12 else ("✓" if key else "sin key")
            providers_info[name] = {
                "name": pcfg.get("name", name),
                "model": pcfg.get("model", ""),
                "base_url": pcfg.get("base_url", ""),
                "api_key_status": masked,
                "has_key": bool(key),
                "active": name == self._preferred_provider,
                "streaming": pcfg.get("streaming", False),
                "websocket_url": pcfg.get("websocket_url", ""),
            }
        last_call = self.get_last_call_info()
        info = {
            "active_provider": self._preferred_provider,
            "fallback_order": self.fallback_order,
            "providers": providers_info,
            "last_call": last_call,
            "total_calls": len(self._call_log),
        }
        # ── Añadir estado del SmartRouter ──
        if self._router:
            info["smart_router"] = self._router.get_routing_status()
        # ── Añadir estado del SynergyRouter ──
        if self._synergy:
            info["synergy_router"] = self._synergy.get_synergy_stats()
        return info

    def call_with_synergy(
        self,
        system_prompt: str,
        user_prompt: str,
        validator: Optional[Callable[[str], bool]] = None,
    ) -> SynergyResult:
        """
        Llamada al LLM con sinergia: validación + corrección automática.

        Flujo:
        1. Proveedor rápido genera un borrador.
        2. Si el validador acepta → retorno inmediato (costo mínimo).
        3. Si falla → el borrador se pasa como contexto a un corrector.
        4. Si el rápido está caído → failover directo al corrector.

        Args:
            system_prompt: Prompt del sistema.
            user_prompt: Prompt del usuario.
            validator: Función que devuelve True si el output es válido.
                      Por defecto: always_valid (sin validación extra).
                      Usar is_valid_json para validar JSON,
                      is_non_empty para rechazar respuestas vacías/degradadas,
                      is_valid_json_with_fields("status", "data") para campos obligatorios.

        Returns:
            SynergyResult con el valor y metadatos de la sinergia.
        """
        if not self._synergy:
            # Sin synergy router — fallback a llamada normal
            text = self.call(system_prompt, user_prompt)
            return SynergyResult(
                value=text, provider=self._preferred_provider or "unknown",
                phase="draft", synergy_used=False, validated=True,
                total_latency_ms=0.0,
            )

        available = self._available_providers()
        if not available:
            raise LLMClientError(
                "No hay proveedores con API key configurada. "
                "Introduce al menos una desde el dashboard (tab Pipeline)."
            )

        result = self._synergy.execute(system_prompt, user_prompt, validator=validator)

        # Log de la llamada sinérgica
        self._call_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": result.provider,
            "model": self.providers.get(result.provider, {}).get("model", ""),
            "input_chars": len(system_prompt) + len(user_prompt),
            "output_chars": len(result.value) if isinstance(result.value, str) else 0,
            "status": "ok" if result.validated else "unvalidated",
            "synergy_used": result.synergy_used,
            "synergy_phase": result.phase,
            "attempts": len(result.attempts),
            "latency_ms": result.total_latency_ms,
        })

        return result

    def call_json_with_synergy(
        self,
        system_prompt: str,
        user_prompt: str,
        required_fields: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """
        Llamada sinérgica que valida que el output sea JSON con campos obligatorios.

        Si el proveedor rápido devuelve JSON roto o sin los campos requeridos,
        la sinergia automáticamente pide a un corrector que lo arregle.

        Args:
            system_prompt: Prompt del sistema.
            user_prompt: Prompt del usuario.
            required_fields: Campos obligatorios en el JSON (ej: ["status", "data"]).
                            Si es None, solo valida que sea JSON parseable.

        Returns:
            Dict con el JSON parseado y validado.
        """
        if required_fields:
            validator = is_valid_json_with_fields(*required_fields)
        else:
            validator = synergy_is_valid_json

        result = self.call_with_synergy(system_prompt, user_prompt, validator=validator)
        return parse_json_response(result.value)

    # ─── STREAMING (SSE vía OpenAI SDK) ──────────────────────────────────

    def call_stream(self, system_prompt: str, user_prompt: str, provider_name: Optional[str] = None):
        """
        Llamada streaming al LLM usando SSE (Server-Sent Events).
        Yields tokens incrementalmente. Si no se especifica proveedor,
        usa el preferido o el primero disponible.
        """
        name = provider_name or self._preferred_provider
        if not name:
            available = self._available_providers()
            if not available:
                raise LLMClientError("No hay proveedores con API key configurada.")
            name = available[0]

        client, pcfg = self._make_client(name)
        model = pcfg.get("model", "")
        self._log(f"stream: {name} / {model}")

        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=pcfg.get("max_tokens", 2048),
            temperature=pcfg.get("temperature", 0.3),
            timeout=pcfg.get("timeout", 120),
            stream=True,
        )

        full_text = ""
        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                full_text += delta.content
                yield delta.content

        # Actualizar estado
        self._preferred_provider = name
        self._log(f"stream ok: {name} - {len(full_text)} chars")
        self._call_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": name,
            "model": model,
            "input_chars": len(system_prompt) + len(user_prompt),
            "output_chars": len(full_text),
            "status": "ok",
            "streaming": True,
        })

    # ─── SYNERGY STREAMING (enrutamiento inteligente + streaming) ─────────

    def call_with_synergy_stream(self, system_prompt: str, user_prompt: str):
        """
        Llamada sinérgica con streaming: SmartRouter elige el mejor proveedor
        y streamea tokens incrementalmente. Si falla, failover al siguiente.

        Flujo:
        1. SmartRouter puntúa proveedores por peso × latencia × éxito × disponibilidad.
        2. El mejor proveedor disponible streamea tokens al cliente.
        3. Si falla, reintenta con backoff y prueba el siguiente proveedor.
        4. Emite metadata de routing para que el frontend sepa qué proveedor se usó.

        Yields: tuplas (token_str, metadata_dict)
        - token_str: el texto del token incremental
        - metadata_dict: {"type": "routing"|"token"|"done", ...}
        """
        available = self._available_providers()
        if not available:
            raise LLMClientError(
                "No hay proveedores con API key configurada. "
                "Introduce al menos una desde el dashboard (tab Pipeline)."
            )

        # ── Usar SmartRouter para elegir el mejor proveedor ──
        if self._router:
            request = {"system_prompt": system_prompt, "user_prompt": user_prompt}
            try:
                decision = self._router.route(request, group="default")
                chosen_provider = decision.target_id
                self._log(f"synergy_stream: SmartRouter eligió {chosen_provider} (score={decision.score:.4f}, reason={decision.reason})")
                # Reordenar available para que el elegido vaya primero
                if chosen_provider in available:
                    available.remove(chosen_provider)
                    available.insert(0, chosen_provider)
            except Exception as exc:
                self._log(f"synergy_stream: SmartRouter falló al decidir, usando orden fallback: {exc}", "WARN")

        # ── Intentar streaming con failover ──
        errors = []
        for name in available:
            try:
                pcfg = self.providers.get(name, {})
                model = pcfg.get("model", "")

                # Emitir metadata de routing
                yield "", {
                    "type": "routing",
                    "provider": name,
                    "model": model,
                    "attempt": len(errors) + 1,
                    "total_available": len(available),
                }

                # Intentar streaming desde este proveedor
                client_obj, pcfg_obj = self._make_client(name)
                stream = client_obj.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=pcfg_obj.get("max_tokens", 2048),
                    temperature=pcfg_obj.get("temperature", 0.3),
                    timeout=pcfg_obj.get("timeout", 120),
                    stream=True,
                )

                full_text = ""
                for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta and delta.content:
                        full_text += delta.content
                        yield delta.content, {"type": "token"}

                # Streaming exitoso
                self._preferred_provider = name
                self._log(f"synergy_stream ok: {name}/{model} - {len(full_text)} chars")

                yield "", {
                    "type": "done",
                    "provider": name,
                    "model": model,
                    "transport": "synergy_sse",
                    "output_chars": len(full_text),
                    "failed_providers": [e[0] for e in errors],
                }
                return  # Éxito — salir del bucle

            except Exception as exc:
                err_msg = str(exc)[:200]
                errors.append((name, err_msg))
                self._log(f"synergy_stream fail: {name} - {err_msg}", "WARN")

                # Emitir metadata de failover
                yield "", {
                    "type": "failover",
                    "failed_provider": name,
                    "error": err_msg,
                    "trying_next": len(errors) < len(available),
                }
                continue

        # Todos los proveedores fallaron
        raise LLMClientError(
            f"Todos los proveedores fallaron en streaming sinérgico:\n"
            + "\n".join(f"  - {name}: {err}" for name, err in errors)
        )

    # ─── WEBSOCKET STREAMING (Modal) ──────────────────────────────────────

    async def call_websocket_stream(self, system_prompt: str, user_prompt: str, provider_name: str = "modal") -> AsyncGenerator[str, None]:
        """
        Streaming via WebSocket directo al endpoint de Modal.
        Conecta a wss://api.us-west-2.modal.direct/v1/chat/completions
        y envía/recibe mensajes JSON en tiempo real.
        """
        if provider_name not in self.providers:
            raise LLMClientError(f"Proveedor '{provider_name}' no encontrado en config")
        pcfg = self.providers[provider_name]
        api_key = self.get_credential(provider_name)
        if not api_key:
            raise LLMClientError(f"API key no configurada para '{provider_name}'")

        ws_url = pcfg.get("websocket_url", "")
        if not ws_url:
            # Fallback: usar SSE streaming si no hay WebSocket URL
            self._log(f"ws: sin websocket_url para {provider_name}, usando SSE fallback")
            for token in self.call_stream(system_prompt, user_prompt, provider_name):
                yield token
            return

        ws_endpoint = ws_url.replace("wss://", "wss://").replace("https://", "wss://") + "/chat/completions"
        model = pcfg.get("model", "")

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": pcfg.get("max_tokens", 2048),
            "temperature": pcfg.get("temperature", 0.3),
            "stream": True,
        }

        headers = [
            ("Authorization", f"Bearer {api_key}"),
            ("Content-Type", "application/json"),
        ]

        self._log(f"ws: conectando a {ws_endpoint} ({model})")
        full_text = ""

        try:
            async with websockets.connect(
                ws_endpoint,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=60,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps(payload))

                async for raw_message in ws:
                    try:
                        data = json.loads(raw_message)

                        # Formato OpenAI streaming via WS
                        if data.get("object") == "chat.completion.chunk":
                            choices = data.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    full_text += content
                                    yield content
                        elif data.get("object") == "chat.completion":
                            # Respuesta completa (no-chunked)
                            choices = data.get("choices", [])
                            if choices:
                                msg = choices[0].get("message", {})
                                content = msg.get("content", "")
                                if content:
                                    full_text += content
                                    yield content
                        elif "error" in data:
                            err = data["error"]
                            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                            self._log(f"ws error: {err_msg}", "ERROR")
                            raise LLMClientError(f"WebSocket error: {err_msg}")

                        # Fin de stream
                        if choices and choices[0].get("finish_reason") == "stop":
                            break

                    except json.JSONDecodeError:
                        # Puede ser SSE formato dentro de WebSocket
                        if raw_message.startswith("data: "):
                            line_data = raw_message[6:].strip()
                            if line_data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(line_data)
                                choices = chunk.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        full_text += content
                                        yield content
                            except json.JSONDecodeError:
                                pass

        except websockets.exceptions.InvalidStatusCode as exc:
            # Si el WS no es soportado, fallback a SSE streaming
            self._log(f"ws: InvalidStatusCode {exc.status_code}, fallback SSE", "WARN")
            for token in self.call_stream(system_prompt, user_prompt, provider_name):
                yield token
            return
        except websockets.exceptions.InvalidHandshake as exc:
            self._log(f"ws: InvalidHandshake {exc}, fallback SSE", "WARN")
            for token in self.call_stream(system_prompt, user_prompt, provider_name):
                yield token
            return
        except Exception as exc:
            self._log(f"ws error: {exc}", "ERROR")
            # Último fallback: llamada normal no-streaming
            self._log(f"ws: fallback a llamada normal", "WARN")
            result = self.call(system_prompt, user_prompt)
            yield result
            return

        self._preferred_provider = provider_name
        self._log(f"ws ok: {provider_name} - {len(full_text)} chars")
        self._call_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": provider_name,
            "model": model,
            "input_chars": len(system_prompt) + len(user_prompt),
            "output_chars": len(full_text),
            "status": "ok",
            "streaming": True,
            "transport": "websocket",
        })


def parse_json_response(raw: str) -> Dict[str, Any]:
    text = raw.strip()
    if "```json" in text:
        start = text.find("```json") + len("```json")
        end = text.find("```", start)
        if end > start: text = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end > start: text = text[start:end].strip()
    elif "{" in text and "}" in text:
        start = text.find("{")
        end = text.rfind("}") + 1
        text = text[start:end]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMClientError(f"Respuesta no es JSON válido: {exc}\nPrimeros 200 chars: {text[:200]}") from exc

    # ── Normalizar: si el LLM no usó el schema esperado, envolverlo ──
    if "status" not in parsed:
        # El LLM devolvió datos sin el wrapper {status, data, audit_note}
        # Intentar envolverlo en el schema esperado
        parsed = {"status": "ok", "data": parsed, "audit_note": "Normalizado automáticamente: el LLM no usó el schema wrapper."}
    elif "data" not in parsed and parsed.get("status") == "ok":
        # Tiene status pero no data — mover el resto a data
        data = {k: v for k, v in parsed.items() if k not in ("status", "audit_note")}
        parsed = {"status": "ok", "data": data, "audit_note": parsed.get("audit_note", "Normalizado: campos movidos a data.")}

    return parsed
