[English](../en/cli.md) | [日本語](../ja/cli.md) | [한국어](../ko/cli.md) | [Français](../fr/cli.md) | [Deutsch](../de/cli.md) | **Español** | [Português (BR)](../pt-BR/cli.md) | [Русский](../ru/cli.md) | [简体中文](../zh-CN/cli.md)

# Referencia de la CLI

Hay dos puntos de entrada, ambos instalados por `uv sync`:

| Comando | Paquete | Propósito |
|---|---|---|
| `factory` | `mao.cli:main` | el plano de control de la fábrica |
| `mrl` | `mrl.cli:main` | el kit de herramientas de MRL (MAO Relation Language): evaluar, comprobar, explicar diagnósticos |

Dentro de los sandboxes, los operadores usan una tercera herramienta, `factoryctl`, descrita en [Runtime § 3](runtime.md#3-dentro-de-una-estación).

Ejecuta cualquier comando con `--help` para ver sus argumentos exactos. La lista normativa de comandos es [`spec/system/commands.md`](../../spec/system/commands.md).

## Opciones globales

| Opción | Variable de entorno | Predeterminado | Significado |
|---|---|---|---|
| `--root DIR` | `FACTORY_ROOT` | `.factory` | directorio de estado de la fábrica |
| `--registry DIR` | `FACTORY_REGISTRY` | `registry` | directorio del registro (Specs) |
| `--provider docker\|local` | | `docker` | proveedor de sandbox |
| `--local-commands FILE` | | | mapa nombre de comando → comando del host para el proveedor local |
| `--epoch COMMIT` | | epoch activa | ejecutar contra una epoch concreta |
| `--json` | | | salida legible por máquinas |
| `--version` | | | mostrar la versión |

## Códigos de salida

| Código | Significado |
|---|---|
| 0 | éxito |
| 1 | fallo de negocio: job `DEAD`, ejecución `FAILED`/`CANCELLED`, veredicto de contrato `fail`, hallazgos de auditoría abiertos |
| 2 | error de uso |
| 3 | registro inválido (`REGISTRY_INVALID`) |
| 4 | error de entorno o de infraestructura, por ejemplo Docker no disponible |
| 5 | error interno |

## Estructura y gobernanza

| Comando | Descripción |
|---|---|
| `check ROOT` | cargar y compilar un registro; código 3 si hay errores |
| `epoch init COMMIT` · `ls` · `show [COMMIT]` · `rollback [COMMIT]` · `adopt COMMIT` | gestionar las epochs y el puntero activo |
| `changeset propose FILE --commit C --by ACTOR` · `ls [--state S]` · `show ID` · `withdraw ID --by ACTOR` · `advance ID` | el pipeline de ChangeSets |
| `governance tick` · `governance safe-mode` | avanzar los ChangeSets una vez; mostrar o evaluar el modo seguro |
| `contract validator …` · `golden …` · `equivalence …` · `diff …` · `sink-dry-run …` · `ls` | pruebas de contrato V4 |
| `audit once` · `audit ls [--state S]` | ejecutar la auditoría de invariantes / listar hallazgos |
| `drift ls` | hallazgos de deriva |

## Material

| Comando | Descripción |
|---|---|
| `bean import --type TYPE --file PATH=HOST …` | admitir archivos externos como un bean; imprime su id |
| `bean lineage BEAN --direction up\|down` | jobs y beans aguas arriba o aguas abajo |
| `bean label BEAN KEY=VALUE` | añadir una etiqueta (las etiquetas pueden disparar líneas) |
| `cache ls [--station S@N]` · `explain JOB` · `invalidate (--key\|--job\|--station\|--bean) … --reason TEXT` | la caché de jobs |
| `gc [--dry-run]` | retención: lápidas, barrido de blobs, recuperación de espacios de trabajo |

## Ejecución

| Comando | Descripción |
|---|---|
| `job run STATION --input PORT=BEAN …` | enviar y ejecutar un job en línea (código 0 sellado, 1 muerto) |
| `job show JOB` · `job exec JOB` | inspeccionar un job; tomar el arrendamiento de un job enviado y ejecutarlo (lo usan los subprocesos worker de serve) |
| `line plan LINE@N` | compilar una línea en su plan de ejecución (nodos, convertidores, presupuestos) |
| `line run LINE@N --in PORT=BEAN … [--budget USD] [--variant STEP=V] [--no-cache]` | iniciar una ejecución y llevarla hasta el final |
| `line ls` · `show RUN` · `nodes RUN` · `cancel RUN` · `retry RUN` · `rebuild RUN` | inspeccionar y controlar ejecuciones |
| `trigger process` | evaluar una vez los disparadores de líneas |
| `schedule tick` · `dlq ls` · `quarantine BEAN --reason TEXT` · `release BEAN` | planificador, mensajes muertos, cuarentena |
| `approval ls [--job J] [--status S]` · `show ID` · `decide ID approve\|deny --by NAME [--note TEXT]` | puntos de aprobación |

## Integración

| Comando | Descripción |
|---|---|
| `image build …` · `image ls` | construir y listar imágenes de máquinas (Dockerfiles construidos por el Foreman) |
| `gateway manifest MACHINE@V` | el manifiesto de la pasarela de servicios para una máquina de forma B |
| `egress check STATION@N` | mostrar el plan de salida de red efectivo de una estación |
| `secret ls` | nombres y clases de los secretos (nunca los valores) |
| `backend status` · `migrate …` · `verify` | backend de almacenamiento: local ↔ S3 + Postgres |
| `backup create` · `verify` · `restore` | copias de seguridad del estado y de los blobs |

## Autonomía

| Comando | Descripción |
|---|---|
| `serve [--once]` | el demonio: disparadores, ejecuciones, jobs, gobernanza, auditoría, canarios, tickets de activación |
| `foreman next` · `done WAKE` · `get KEY` · `list KIND` · `diff BASE COMMIT` · `bean …` · `job …` · `dlq` · `drift` · `invariant` · `metrics` · `approval` · `changeset` · `review` · `conversion` · `supervise` · `sessions` · `wakes` · `reopen` | la superficie de trabajo del Foreman (capataz) |
| `mcp --role foreman\|reviewer` | la misma superficie como servidor MCP sobre stdio |
| `maintain bootstrap` | comprobar que existen las líneas de mantenimiento y `registry-sink`; si no, código 1 y lista de lo que falta |

## Observabilidad

| Comando | Descripción |
|---|---|
| `status` | una instantánea JSON de toda la fábrica |
| `metrics` | formato de texto de Prometheus |
| `events [--since SEQ] [--kind K] [--limit N]` | el registro de eventos |
| `console [--port N]` | consola web local de solo lectura (por defecto `127.0.0.1:8090`) |

## `mrl`

| Comando | Descripción |
|---|---|
| `mrl eval EXPR` · `mrl template TEXT` | evaluar una expresión / renderizar una plantilla |
| `mrl check MODULE` · `mrl run MODULE` | comprobar estáticamente / evaluar un módulo de reglas |
| `mrl registry …` | comprobaciones y consultas del registro (`mrl registry check --strict`) |
| `mrl resolve …` | resolver conversiones para una arista hipotética |
| `mrl explain CODE` | explicar un código de diagnóstico como `E-NO-CONVERTER` |
| `mrl conformance` · `mrl coverage` | ejecutar la batería de conformidad / mostrar la cobertura de los casos de fábrica |
