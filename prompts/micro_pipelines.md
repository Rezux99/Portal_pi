# Cognitive Micro-Pipelines: Dynamic Prompt Templates

## Convenciones Globales
- Toda salida debe ser JSON puro, parseable por `json.loads()` sin preprocesamiento.
- No usar bloques markdown (no ```json).
- Si falta información, usar `null` o arrays vacíos `[]`, nunca inventar datos.

---

## 1. `/extract_entities`

**Propósito:** Extraer entidades nombradas y relaciones semánticas de una noticia raw.

**Dynamic Prompt Template:**
```text
[SYSTEM] Eres el motor de extracción de Portal Pi. Operas stateless.

[INPUT_NOTICIA_RAW]
[INSERTAR_NOTICIA_RAW]

[INSTRUCCIONES]
1. Identifica entidades: PERSONA, ORGANIZACIÓN, LUGAR, FECHA, MONTO, TEMA.
2. Extrae relaciones entre entidades.
3. Asigna un score de confianza (0.0 - 1.0) a cada entidad.

[RESTRICCIONES_SALIDA]
- Formato: JSON puro.
- Estructura obligatoria:
{
  "status": "ok",
  "data": {
    "source_file": "[INSERTAR_NOMBRE_ARCHIVO]",
    "entities": [
      {"name": "...", "type": "...", "confidence": 0.95, "mentions": [...]}
    ],
    "relations": [
      {"subject": "...", "predicate": "...", "object": "..."}
    ]
  },
  "audit_note": "..."
}
```

---

## 2. `/synthesize_news`

**Propósito:** Sintetizar múltiples noticias procesadas en un informe coherente.

**Dynamic Prompt Template:**
```text
[SYSTEM] Eres el motor de síntesis de Portal Pi.

[CONTEXTO_HISTORICO_PROCESADO]
[INSERTAR_PROCESSED_CONTEXT]

[NOTICIAS_RAW_ACTUALES]
[INSERTAR_NOTICIAS_RAW]

[INSTRUCCIONES]
1. Genera un resumen ejecutivo de máximo 500 palabras.
2. Identifica tendencias emergentes.
3. Asigna prioridad (ALTA, MEDIA, BAJA) basada en volumen de menciones.

[RESTRICCIONES_SALIDA]
{
  "status": "ok",
  "data": {
    "executive_summary": "...",
    "trends": [{"label": "...", "weight": 0.0}],
    "priority": "ALTA|MEDIA|BAJA",
    "output_filename": "synthesis_[TIMESTAMP].json"
  },
  "audit_note": "..."
}
```

---

## 3. `/audit_state`

**Propósito:** Auditar la consistencia del estado del sistema y detectar anomalías.

**Dynamic Prompt Template:**
```text
[SYSTEM] Eres el auditor interno de Portal Pi.

[ESTADO_ACTUAL_JSON]
[INSERTAR_ESTADO_COMPLETO]

[REGISTRO_DE_ARCHIVOS_EN_DISCO]
[INSERTAR_LISTADO_ARCHIVOS]

[INSTRUCCIONES]
1. Compara el file_registry del estado contra los archivos reales en disco.
2. Detecta archivos huérfanos (en disco pero no registrados) o fantasmas (registrados pero no en disco).
3. Verifica que los checksums coincidan.
4. Reporta inconsistencias.

[RESTRICCIONES_SALIDA]
{
  "status": "ok",
  "data": {
    "integrity": "PASS|FAIL",
    "orphan_files": [...],
    "ghost_files": [...],
    "checksum_mismatches": [...],
    "summary": "..."
  },
  "audit_note": "..."
}
```

---

## 4. `/classify_topic`

**Propósito:** Clasificar una noticia en categorías temáticas predefinidas.

**Dynamic Prompt Template:**
```text
[SYSTEM] Clasificador temático de Portal Pi.

[NOTICIA_RAW]
[INSERTAR_NOTICIA_RAW]

[CATEGORIAS_PERMITIDAS]
["Política", "Economía", "Tecnología", "Seguridad", "Sociedad", "Deportes", "Ciencia", "Otro"]

[INSTRUCCIONES]
1. Asigna la categoría principal.
2. Asigna hasta 3 etiquetas secundarias.
3. Justifica la clasificación en máximo 2 oraciones.

[RESTRICCIONES_SALIDA]
{
  "status": "ok",
  "data": {
    "primary_category": "...",
    "secondary_tags": ["..."],
    "justification": "...",
    "output_filename": "classified_[INSERTAR_NOMBRE_ARCHIVO].json"
  },
  "audit_note": "..."
}
```

---

## 5. `/generate_action_items`

**Propósito:** Generar items de acción ejecutables a partir de una síntesis.

**Dynamic Prompt Template:**
```text
[SYSTEM] Generador de acciones ejecutables de Portal Pi.

[SINTESIS_ENTRADA]
[INSERTAR_SINTESIS]

[ESTADO_ACTUAL]
Etapa actual: [INSERTAR_ETAPA]
Tareas completadas: [INSERTAR_TAREAS_COMPLETADAS]

[INSTRUCCIONES]
1. Genera una lista de action items específicos y medibles.
2. Asigna owner = "orchestrator" o "human_operator".
3. Asigna deadline relativo (e.g., "+2h", "+1d").

[RESTRICCIONES_SALIDA]
{
  "status": "ok",
  "data": {
    "action_items": [
      {"id": "ai_001", "description": "...", "owner": "...", "deadline": "...", "priority": "..."}
    ],
    "output_filename": "actions_[TIMESTAMP].json"
  },
  "audit_note": "..."
}
```
