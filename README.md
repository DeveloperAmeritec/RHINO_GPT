# rhinogpt
ChatBot académico para estudiantes del Colegio Ameritec

## Variables de entorno

La Lambda necesita estas variables:

```txt
API_OPENAI=...
MODEL=...
MAX_NUM_RESULTS=...
MAX_OUTPUT_TOKENS=...
MEMORY_MINUTES=...
DEFAULT_MONTHLY_LIMIT=100
VS_STUDENT=...
VS_PERSONAL_PROFILES={"profile_id":"vs_..."}
DEFAULT_PERSONAL_PROFILE_ID=3_5
PROMPT_FILE_STUDENT=rhinogpt_student.txt
PROMPT_FILE_PERSONAL=rhinogpt_personal.txt
POLICY_PROCESS_MAP_FILE=policy_process_map_context.txt
LOG_PREVIEW_CHARS=800
```

El mapa de políticas y los documentos institucionales se generan y conservan localmente; no se versionan porque contienen información interna.

## Datos locales y recuperación

Toda la información que no debe llegar a Git se concentra bajo
`local_private/`. Esa carpeta completa debe copiarse por separado al disco de
respaldo y siempre fuera del contenido versionado:

- `local_private/knowledge/ameritec_presencial_2026/`: conocimiento estudiantil vigente.
- `local_private/knowledge/personal/`: políticas y procesos para perfiles del personal.
- `local_private/personal_matrix.xlsx`: asignación privada a perfiles.
- `local_private/backups/`: DynamoDB y evidencia de vector stores.
- `local_private/outputs/`: resultados regenerables de scripts y analítica.

El mapa compacto `app/prompts/policy_process_map_context.txt` sí se versiona:
solo relaciona nombres de políticas y procesos y no contiene el contenido de
los documentos fuente. Se regenera desde la matriz privada mediante
`scripts/generate_policy_process_prompt_context.py`.

La estructura y recuperación de las tablas `Users` y `Chats` se documenta en
`local_private/notebooks/database/rhinogpt_database_recovery.ipynb`. El notebook permite crear
las tablas vacías, generar un backup o restaurarlo. El respaldo no se sube a Git.

`local_private/notebooks/analytics/rhinogpt_indicador_por_unidad.ipynb` genera el indicador de
uso por unidad académica; sus resultados se guardan en
`local_private/outputs/analytics/` y no se versionan.

Antes del primer commit en un repositorio nuevo, ejecuta:

```bash
python3 scripts/check_no_secrets.py
```

Si usas `pre-commit`, instala el hook con `pre-commit install`. La configuración
incluida bloquea patrones comunes de claves OpenAI/AWS, llaves privadas y
credenciales literales. Ningún detector sustituye una revisión del `git diff`
antes de publicar.

`type` define qué configuración se usa:

- `student`: usa `VS_STUDENT` y `PROMPT_FILE_STUDENT`.
- `personal`: usa `VS_STUDENT`, el vector store de `VS_PERSONAL_PROFILES` según `personal_profile_id`, y `PROMPT_FILE_PERSONAL`.

El ruteo queda así:

```txt
student
  prompt: PROMPT_FILE_STUDENT
  vector stores: [VS_STUDENT]

personal
  prompt: PROMPT_FILE_PERSONAL
  vector stores: [VS_STUDENT, VS_PERSONAL_PROFILES[personal_profile_id]]
  fallback profile: DEFAULT_PERSONAL_PROFILE_ID
```

## Búsqueda web y referencias

RhinoGPT usa la Responses API con dos herramientas en modo automático:

- `file_search`: consulta el conocimiento institucional de los vector stores.
- `web_search`: consulta información externa cuando la pregunta requiere referencias, enlaces, bibliografía, información actualizada o exploración en internet.

Cuando se usa `web_search`, los prompts solicitan referencias en texto plano, con una referencia por párrafo y el enlace junto a su descripción. El formato esperado es:

```txt
Khan Academy: curso introductorio de derivadas 🔗 https://www.khanacademy.org/math/calculus
```

El frontend recibe la respuesta en el campo `respuesta`. Actualmente puede mostrarla como texto plano; el emoji `🔗` ayuda a identificar los enlaces mientras se implementa el renderizado de URLs clicables.

Cada chat completado registra en la tabla `Chats` el campo `search_type`, que indica qué conocimiento se utilizó:

- `web_search`
- `file_search`
- `web_search_and_file_search`
- `none`

El mismo valor se incluye en los eventos de CloudWatch junto con `used_web_search` y `used_file_search`.

El `type` enviado en el POST representa el canal solicitado, pero DynamoDB define el acceso efectivo:

- Si un usuario existe como `personal`, responde como `personal` aunque el canal mande `student`.
- Si un usuario existe como `student` y luego llega por canal `personal`, se promueve a `personal` con `personal_profile_id=3_5`.
- Si un usuario no existe, se crea con el tipo solicitado por el canal.

Para estudiantes, si el usuario existe en DynamoDB y tiene `grade`, ese valor se manda al prompt. Si el usuario no existe o no tiene grado, RhinoGPT usa `unknown` y continúa respondiendo.

Si el usuario no existe en DynamoDB, RhinoGPT lo crea automáticamente con el esquema base de la tabla `Users`: `user_id`, `full_name`, `user_type`, `monthly_limit`, `current_usage`, `reset_date`, `registration_date` y `last_access_date`. Para `student` también agrega `grade` y `section` como `NULL`. Para `personal` agrega `personal_profile_id=3_5` por defecto. Después de responder, incrementa `current_usage`.

El payload recomendado es en inglés:

```json
{
  "question": "¿Cuál es la historia de Ameritec?",
  "user": "usuario@ejemplo.edu",
  "type": "student"
}
```

Por compatibilidad temporal, la API todavía acepta los campos antiguos en español:

- `question` o `pregunta`
- `user` o `usuario`
- `type` o `tipo`

Si vienen ambos nombres para el mismo campo, se usa el valor en inglés. Los únicos valores válidos para `type` son `student` y `personal`.

## Archivos locales

`local_private/` contiene insumos privados, manuales, notebooks, respaldos y pruebas. Se conserva en el clon local y está excluida completamente de Git. `local/` también está ignorada para futuros experimentos. Los enlaces a manuales locales solo funcionan si se cuenta con esa carpeta.

Verifica `python3 scripts/check_no_secrets.py` y `git diff --cached` antes de publicar.

## Despliegue y acceso

`app/deploy.template.sh` requiere configuración por variables de entorno. El despliegue configurado anterior permanece en `local_private/deploy.sh`.

El endpoint debe estar protegido por un backend confiable que valide la identidad y el canal: el payload permite registrar o promover usuarios como `personal`. La Lambda no autentica por sí misma al usuario declarado en el POST. No exponer directamente sin resolver esa validación.
