# Portal Pi

Inteligencia periodística multi-agente: ingesta, análisis y síntesis automatizada de noticias con orquestación LLM.

## Arquitectura

```
RSS Feeds → Ingesta → Extracción → Clasificación → Síntesis → Informes
                ↑                                           ↓
         Smart Router ←→ 7 LLM Providers (failover + circuit breaker)
                ↑
         Synergy Router (validación cruzada multi-modelo)
```

### Stack

- **Backend**: Python / FastAPI / Uvicorn
- **Frontend**: SPA (HTML + CSS + JS, sin build step)
- **LLM**: OpenAI-compatible API con 7 proveedores
- **Auth**: Supabase (opcional) con fallback a modo local
- **Storage**: SQLite local + Supabase (opcional)

## Proveedores LLM

Portal Pi usa múltiples proveedores gratuitos con failover automático:

| Proveedor | Modelo | Capa |
|---|---|---|
| Groq | Llama 3.1 8B | Extracción (velocidad) |
| Cerebras | Llama 3.1 8B | Extracción (velocidad) |
| Google Gemini | Gemini 2.0 Flash | Análisis + Síntesis |
| NVIDIA NIM | Llama 3.1 8B | Análisis |
| OpenRouter | Llama 3.1 8B (free) | Análisis |
| Together AI | Llama 3.1 8B Turbo | Crítica |
| Modal | GLM-5.1 FP8 | Síntesis (streaming) |

### Routing

- **Smart Router**: pondera latencia × tasa de éxito × peso por rol, con circuit breaker y backoff
- **Synergy Router**: un modelo rápido genera borrador, un segundo valida/corrige

## Instalación

```bash
git clone https://github.com/Rezux99/Portal_pi.git
cd Portal_pi
pip install -r requirements.txt
```

### Configuración

1. Copia `.env.example` a `.env` y rellena los valores de Supabase (opcional)
2. Arranca el dashboard:
   ```bash
   python scripts/run_dashboard.py
   ```
3. Abre `http://localhost:8420`
4. En **Config → Proveedores LLM**, introduce tus API keys

Las keys se cifran con Fernet y se guardan en `config/.credentials.json`. La clave de cifrado está en `config/.cred_key`. **Ninguno de estos archivos se sube a Git.**

## Uso

### Dashboard

- **Noticias**: fuentes RSS con ingesta on-demand
- **Pipeline**: ejecución simple, multi-agente u orquestada
- **Datos**: entidades, relaciones, síntesis, clasificaciones, acciones
- **Informes**: generación y visualización de informes de inteligencia
- **Chat**: conversación directa con el LLM (WebSocket + REST)
- **Config**: gestión de API keys, router, feeds, logs

### API

| Endpoint | Método | Descripción |
|---|---|---|
| `/api/status` | GET | Estado del sistema |
| `/api/llm/config` | GET | Configuración LLM |
| `/api/llm/credentials` | POST | Guardar API key |
| `/api/llm/credentials/{provider}` | DELETE | Eliminar API key |
| `/api/llm/test` | POST | Test de conexión LLM |
| `/api/ingest` | POST | Ingestar noticias |
| `/api/pipeline/run` | POST | Ejecutar pipeline |
| `/api/pipeline/multi-agent` | POST | Pipeline multi-agente |
| `/api/pipeline/orchestrated` | POST | Pipeline orquestado |
| `/api/raw_news` | GET | Noticias crudas |
| `/api/reports` | GET | Informes generados |
| `/api/feeds` | GET/POST | Feeds RSS |
| `/ws/chat` | WebSocket | Chat en tiempo real |

## Seguridad

- API keys **cifradas en disco** con Fernet (librería `cryptography`)
- Archivos `config/.credentials.json` y `config/.cred_key` excluidos de Git
- Variables de entorno en `.env` excluidas de Git
- Auth Supabase opcional con modo local transparente

## Estructura del proyecto

```
portal_pi/
├── config/           # Configuración (llm.json, feeds.json, .credentials.json)
├── data/             # Datos generados por el pipeline (gitignored)
├── logs/             # Logs del sistema (gitignored)
├── scripts/          # Lógica central
│   ├── dashboard.py  # API del dashboard
│   ├── llm_client.py # Cliente LLM multi-provider con cifrado
│   ├── smart_router.py
│   ├── synergy_router.py
│   ├── ingester.py
│   ├── scheduler.py
│   └── ...
├── server/           # App v2 (FastAPI modular)
├── static/           # CSS + JS del dashboard
├── templates/        # HTML del dashboard
├── supabase/         # Migraciones SQL
└── hybrid_orchestrator/  # Orquestador híbrido
```

## Licencia

MIT
