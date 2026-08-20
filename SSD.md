# Portal Pi — System Design Document (SSD)
## 1. VIRTUAL FILE SYSTEM & DATA FLOW

### 1.1 Árbol de Directorios

```
portal_pi/
├── config/
│   └── (configuración estática: umbrales, credenciales LLM, etc.)
├── data/
│   ├── raw_news/          # Noticias de entrada (txt, md)
│   ├── processed/           # JSONs con entidades y metadatos extraídos
│   ├── synthesized/         # Informes de síntesis generados por el LLM
│   ├── entities/            # Extracciones atómicas de entidades
│   └── audit_logs/          # Logs de auditoría generados por /audit_state
├── logs/
│   └── orchestrator.log     # Traza de ejecución del orquestador
├── prompts/
│   └── micro_pipelines.md   # Templates de prompts dinámicos
├── scripts/
│   ├── __init__.py
│   ├── state_manager.py     # Gestión atómica de memoria_proyecto.json
│   └── main.py              # Bucle CMD y orquestador principal
├── state/
│   └── memoria_proyecto.json # Cerebro persistente (JSON de estado)
├── requirements.txt
└── (este documento SSD)
```

### 1.2 Flujo de Datos I/O

```
┌─────────────────┐     Lee archivos .txt/.md      ┌──────────────────┐
│  data/raw_news  │ ───────────────────────────────> │                  │
└─────────────────┘                                │   ORQUESTADOR    │
                                                   │     main.py      │
┌─────────────────┐     Lee JSON de estado        │                  │
│ state/memoria_  │ <────────────────────────────> │  Ensambla Prompt │
│ proyecto.json   │   Escribe parches atómicos     │   Dinámico       │
└─────────────────┘                                └────────┬─────────┘
                                                            │
                                                            │ Inyecta variables
                                                            │ desde disco
                                                            ▼
                                                   ┌──────────────────┐
                                                   │   LLM (GLM-5.1)  │
                                                   │  (llamada API    │
                                                   │   simulada en    │
                                                   │   desarrollo)    │
                                                   └────────┬─────────┘
                                                            │
                                                            │ Respuesta JSON
                                                            │ estricta
                                                            ▼
                                                   ┌──────────────────┐
                                                   │   RUTEADOR       │
                                                   │   route_response │
                                                   └────────┬─────────┘
                                                            │
                    ┌──────────────────┐                    │
                    │ data/processed/  │ <──────────────────┘
                    │ data/synthesized/│
                    │ data/entities/   │
                    └──────────────────┘
```

**Secuencia de Flujo:**
1. El operador ingresa un comando en el CLI (`/extract_entities`, `/synthesize_news`, etc.).
2. `main.py` instancia `StateManager`, que lee `memoria_proyecto.json` desde disco con bloqueo compartido.
3. `assemble_dynamic_prompt()` lee los archivos relevantes del VFS (`raw_news`, `processed`, `state`) y ensambla un prompt masivo con marcadores de inyección (`[INSERTAR_NOTICIA_RAW]`, `[INSERTAR_ESTADO_COMPLETO]`).
4. Se invoca `call_llm(prompt)`. En producción, esto realiza una solicitud HTTP a la API de GLM-5.1.
5. La respuesta del LLM debe ser un JSON puro con la estructura: `{"status": "ok|error", "data": {...}, "audit_note": "..."}`.
6. `route_response()` parsea el JSON, determina la acción correspondiente al comando, y:
   - Escribe archivos de salida en el VFS (`data/entities/`, `data/synthesized/`).
   - Actualiza `memoria_proyecto.json` mediante parches atómicos (`patch_state`, `register_file`, `append_audit`).
   - Mueve o marca archivos raw como procesados actualizando el `file_registry` y el array `processed_checksums`.
7. El bucle CLI vuelve al estado `IDLE`, esperando el siguiente comando.

---

## 2. STATE MACHINE SCHEMA

Archivo: `state/memoria_proyecto.json`

### 2.1 Descripción de Campos

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `schema_version` | string | Versión del esquema para migraciones futuras. |
| `session` | object | Metadatos de la sesión activa. |
| `execution_pointers` | object | Punteros de ejecución: qué archivo se está procesando y en qué etapa. |
| `pipeline_stages` | object | Máquina de estados finita. Cada etapa define `next` (transición válida) y `description`. |
| `file_registry` | object | Registro de archivos conocidos por categoría (`raw_news`, `processed`, `synthesized`). |
| `flags` | object | Flags de control global (`global_status`, `allowed_transitions`). |
| `audit_trail` | array | Traza inmutable de eventos significativos con timestamp. |

### 2.2 Estados de la Máquina de Estados

```
IDLE ──> EXTRACTION ──> SYNTHESIS ──> AUDIT ──> ARCHIVE ──> IDLE
  ^                                                              |
  └──────────────────────────────────────────────────────────────┘

ERROR (transición de escape desde cualquier estado)
```

- **IDLE**: Sistema en espera. Punteros nulos.
- **EXTRACTION**: Leyendo raw_news, extrayendo entidades.
- **SYNTHESIS**: Consolidando entidades y raw_news en un informe.
- **AUDIT**: Verificando integridad del estado contra el disco.
- **ARCHIVE**: Moviendo resultados finales y liberando punteros.
- **ERROR**: Estado de recuperación. Requiere intervención manual o `/audit_state`.

### 2.3 Flags

- `global_status`: `PENDING | PROCESSING | COMPLETED | ERROR`
- `allowed_transitions`: Array de etapas a las que se permite transicionar manualmente o automáticamente.

---

## 3. CORE ORCHESTRATOR CODE

### 3.1 state_manager.py

Módulo de gestión de estado con las siguientes garantías:
- **Atomicidad**: Escrituras mediante `temp -> backup -> overwrite`.
- **Concurrencia**: Bloqueos de archivo (`fcntl.LOCK_SH` / `fcntl.LOCK_EX`).
- **Recuperación**: Backup automático antes de cada sobrescritura.
- **Parches parciales**: `patch_state()` fusiona recursivamente sin destruir el resto del JSON.

Clases principales:
- `StateManagerError`: Excepción base para errores irrecuperables.
- `StateManager`: Interfaz única para leer/escribir el cerebro persistente.

### 3.2 main.py

Bucle CLI con arquitectura de comandos:
- `PortalPiCLI`: Clase orquestadora que mapea comandos a handlers.
- `assemble_dynamic_prompt()`: Función pura que construye el prompt leyendo el VFS.
- `call_llm()`: Interfaz simulada para la API de GLM-5.1.
- `route_response()`: Enrutador que persiste la salida del LLM en disco y actualiza el estado.

---

## 4. COGNITIVE MICRO-PIPELINES

Ver archivo: `prompts/micro_pipelines.md`

Resumen de comandos implementados:

| Comando | Propósito | Output Esperado |
|---------|-----------|-----------------|
| `/extract_entities` | Extraer entidades y relaciones de raw_news | `data/entities/*.json` |
| `/synthesize_news` | Generar informe ejecutivo consolidado | `data/synthesized/*.json` |
| `/audit_state` | Auditar integridad estado vs disco | Entrada en `audit_trail` |
| `/classify_topic` | Clasificar noticia en categorías | Metadatos en `file_registry` |
| `/generate_action_items` | Crear tareas accionables desde síntesis | JSON de action items |

---

## 5. PROTOCOLOS DE SEGURIDAD Y ROBUSTEZ

1. **Sin estado en RAM**: El orquestador no mantiene variables de estado globales. Cada ciclo lee del disco.
2. **Idempotencia**: Reejecutar `/extract_entities` sobre el mismo archivo raw no duplica salida gracias a `processed_checksums`.
3. **Inmutabilidad del audit_trail**: Solo operaciones de `append`. Nunca se borra.
4. **Graceful Degradation**: Si el LLM devuelve JSON inválido, el sistema captura `JSONDecodeError`, loggea el error y mantiene el estado anterior intacto.
5. **Transacciones de archivo**: Todo movimiento de archivos entre `raw_news` y `processed` se hace mediante `os.replace` (atómico en POSIX).
