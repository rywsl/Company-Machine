[English](../en/concepts.md) | [日本語](../ja/concepts.md) | [한국어](../ko/concepts.md) | [Français](../fr/concepts.md) | [Deutsch](../de/concepts.md) | **Español** | [Português (BR)](../pt-BR/concepts.md) | [Русский](../ru/concepts.md) | [简体中文](../zh-CN/concepts.md)

# Conceptos

Machine Agent Org tiene un metamodelo pequeño. Todo lo que puedes construir (un pipeline de vídeo, una línea de revisión de código, un informe nocturno) se expresa con las siete primitivas de abajo, escritas como **Specs** en YAML dentro de un registro Git. El compilador de estructura comprueba esas Specs igual que un comprobador de tipos comprueba código.

> Justificación del diseño: secciones 3–10 de [`docs/architecture.md`](../architecture.md) (en chino). Reglas formales: [`spec/mrl/factory-profile.md`](../../spec/mrl/factory-profile.md).

## 1. Las primitivas

| Primitiva | Significado | Puntos clave |
|---|---|---|
| **Type** (tipo) | la especificación de un material | `name@major.minor`. Clases: atómico, `bundle{…}`, `list<T>`, `ref<T>`, `any`. Incluye una disposición de la carga útil, un esquema de metadatos, validadores (con fixtures de aceptación y rechazo), *hechos* medidos y *propiedades* con nombre |
| **Bean** (material) | una pieza de material | direccionado por contenido (`sha256:…`), inmutable, sellado con su linaje y su procedencia |
| **Machine** (máquina) | un producto existente | descrita por un Machine Package: imagen fijada por digest, recetas, entradas y salidas, sondas, referencias de conformidad |
| **Operator** (operador) | quien maneja la máquina | un agente CLI (Claude Code headless, Codex), un script o una persona a través de una bandeja de entrada |
| **Station** (estación) | Recipe × Operator × Policy | puertos tipados. Roles: `source`, `transform`, `sink`; `converter`, `judge` y `planner` son transformaciones especializadas |
| **Line** (línea) | un DAG tipado de estaciones | más construcciones dinámicas acotadas. Una línea con interfaz puede usarse como estación (`subline`) |
| **Policy** (política) | permisos | nivel de salida de red, secretos, presupuesto, aprobaciones, publicación, retención |

Por encima hay tres conceptos de nivel meta. Una **Spec** es cualquiera de las definiciones anteriores, almacenada como un bean de tipo `spec.<kind>@N`. Un **Invariant** es una regla de la constitución escrita en MRL. Un **ChangeSet** es un diff de Specs y es, a su vez, un bean. Como la propia estructura de la fábrica es material, las mismas comprobaciones pueden proteger el trabajo de mantenimiento. Así se mantiene en orden al Foreman (capataz).

## 2. Tipos, hechos y propiedades

Un tipo declara *hechos*, que miden los validadores, y *propiedades*, que son expresiones MRL con nombre sobre esos hechos.

```yaml
# spec/examples/registry/types/video.yaml (extracto)
kind: Type
metadata: {name: video.mp4, version: "1.3"}
spec:
  extends: video@1
  payload:
    - {path: video.mp4, media_type: video/mp4, required: true}
  validators:
    - name: ffprobe-facts
      run: [probe-video, /bean/payload/video.mp4]
      emits: [duration_s, width, height, fps, vcodec, audio_streams, mean_volume_db]
      fixtures:
        accept: [fixtures/video/ok-1920x1080.mp4]
        reject: [fixtures/video/truncated.mp4, fixtures/video/png-renamed.mp4]
  properties:
    has_audio: "bean.facts.audio_streams > 0"
    vertical:  "bean.facts.height > bean.facts.width"
    hd:        "(bean.facts.width < bean.facts.height ? bean.facts.width : bean.facts.height) >= 1080"
```

Cada validador incluye fixtures de aceptación y de rechazo. Un cambio que haga pasar un fixture de rechazo, y que por tanto dejaría pasar material defectuoso, se clasifica automáticamente como de alto riesgo.

## 3. Contratos de consumidor: `guarantees` y `requires`

Los puertos expresan contratos en términos de propiedades. Un puerto de salida enumera lo que **garantiza** (`guarantees`), y el gate (control de salida) lo hace cumplir. Un puerto de entrada enumera lo que **requiere** (`requires`).

```yaml
# spec/examples/registry/stations/vertical.yaml
kind: Station
metadata: {name: vertical, version: 1}
spec:
  role: transform
  machine: ffmpeg@3
  recipe: reframe-vertical
  operator: {runtime: script@1}
  inputs:  {video: {type: video.mp4@1, requires: [hd, has_audio]}}
  outputs: {video: {type: video.mp4@1, guarantees: [vertical, hd, h264, has_audio, not_silent]}}
  limits:  {timeout: 15m, budget_usd: 0}
  policy: tool-default@1
```

En tiempo de compilación, cada arista debe cumplir `requires ⊆ guarantees ∪ type properties`. Es una inclusión de conjuntos: cuando una estación aguas arriba elimina una garantía, el compilador indica exactamente qué puertos aguas abajo se rompen. En tiempo de ejecución, la comprobación de entrada evalúa las mismas propiedades sobre el bean real.

## 4. Conversión: los productos se convierten en material automáticamente

Cuando los tipos o las propiedades de una arista no encajan, el compilador busca en el registro estaciones de **conversión** (`converter`) e inserta exactamente una cadena. Si hay varias cadenas válidas, elige según estas reglas, en orden: primero sin pérdida, después la de menos pasos, después la de menor coste estimado, después la de menor tiempo estimado y, por último, por nombre. Lo viste en [Primeros pasos](getting-started.md): `note.raw@1 → note.text@1` se enlazó con `note-to-text@1`. Cuando no existe ninguna cadena, el compilador no adivina. Informa de `E-NO-CONVERTER`, y una *solicitud de conversión* despierta al Foreman, que puede proponer un convertidor nuevo.

## 5. Líneas

```yaml
# spec/examples/registry/lines/explainer-multilang.yaml (extracto)
kind: Line
metadata: {name: explainer-multilang, version: 5}
spec:
  interface:
    inputs: {brief: {type: brief.video@1}}
  trigger:
    - {on: bean, type: brief.video@1, label: "line=explainer-multilang"}
  budget: {cap_usd: 12.0}
  steps:
    explainer: {station: explainer@3, in: {brief: $inputs.brief}}
    dub_en:    {station: dub@2, params: {target_language: en},
                in: {video: explainer.video, subtitle: explainer.subtitle}}
    vertical:  {station: vertical@1, in: {video: dub_en.video}}
    to_s3:     {station: publish-s3@2, params: {bucket: media-archive, prefix: "explainers/{{ run.id }}/"},
                in: {artifact: vertical.video}}
```

Las reglas:
- Todo producto terminal debe consumirlo un sink (estación de publicación) o figurar en `keep`.
- Los presupuestos son jerárquicos: global → línea → ejecución → estación → job.
- Los disparadores pueden ser llegadas de `bean`, programaciones `cron` o `every`, o lanzamientos manuales.

### Estructura dinámica acotada

| Construcción | Qué hace | Límite |
|---|---|---|
| `map` | ejecuta un subpaso por cada elemento de una `list<T>` | `max_items` (rechazar o truncar si se supera), `max_parallel` |
| `switch` | elige una rama según una condición MRL | debe ser exhaustivo, con un `default` |
| `until` | repite un subpaso hasta que se cumple una condición MRL | `max_iter`; agotarlo hace fallar la ejecución |
| `subline` | ejecuta otra línea dentro de la epoch de esta ejecución | límite de profundidad |
| `plan` | una estación **planner** emite un `Plan` (un sub-DAG) | se compila con V1/V2 y se contrasta con `plan_bounds` antes de que se ejecute nada |

## 6. Epochs: cambiar sin romper el trabajo en curso

Cada commit del registro que supera la verificación puede convertirse en una **epoch** (una versión congelada de la estructura). Una ejecución se vincula a una epoch al empezar y la conserva hasta terminar, de modo que cambiar la estructura nunca afecta al trabajo en curso. Hacer rollback significa mover hacia atrás el puntero de la epoch actual. Puede haber varias epochs en servicio a la vez, por ejemplo la epoch activa junto a un canario.

## 7. Seis capas de verificación

| Capa | Qué se comprueba | Cuándo | Si falla |
|---|---|---|---|
| **V1** Spec | esquema, referencias, versiones fijadas, lint de políticas | en cada propuesta | se rechaza el cambio |
| **V2** Estructura | tipos y contratos de las aristas, ciclos, entradas obligatorias, salidas consumidas, permisos, presupuesto, límites dinámicos | propuesta + antes de cada ejecución | se rechaza el cambio / la ejecución |
| **V3** Evolución | compatibilidad con la versión anterior, todos los contratos de los consumidores aguas abajo, radio de impacto | propuesta | se rechaza, o se exige un convertidor de migración |
| **V4** Pruebas de contrato | conformidad de las máquinas, briefs de referencia, viajes de ida y vuelta de los convertidores, ensayos en seco de los sinks | propuesta + canarios programados | se rechaza, o se lanza una alerta de deriva |
| **V5** Runtime | comprobación de entrada, gate (estructura → validador → aceptación → juez), verificación de los recibos de los sinks | cada job | rechazo → reintento, o DLQ |
| **V6** Continua | auditoría de invariantes, vigilancia de la deriva, integridad del CAS y del linaje, reconciliación | periódica + eventos | cuarentena, cortacircuitos, rollback, despertar al Foreman |

## 8. Cuatro tipos de deriva

| Deriva | Ejemplo | La detecta |
|---|---|---|
| Deriva de spec | alguien edita un tipo o un puerto | V1–V3 y los contratos de consumidor |
| Deriva de comportamiento | el producto se actualiza, cambia el modelo o el prompt | versiones fijadas, canarios V4, ejecuciones shadow |
| Deriva semántica | la salida sigue cumpliendo el esquema, pero la calidad empeora | expresiones de aceptación, jueces por muestreo, métricas de referencia |
| Deriva de configuración | contenedores o vinculaciones de secretos editados fuera del proceso | el reconciliador y la auditoría de invariantes |

## 9. Niveles de gobernanza

El pipeline de verificación deriva de forma determinista un nivel de riesgo a partir del diff de cada ChangeSet. Nadie puede rebajarlo, ni siquiera el Foreman.

| Nivel | Cambio típico | Requisitos |
|---|---|---|
| T0 | reintento, cuarentena | automático |
| T1 | ajuste de plantilla, nuevo convertidor, subida menor de versión de un tipo | verificación + ejecución shadow |
| T2 | incorporar o actualizar una máquina, línea nueva, subida mayor de versión de un tipo | + canario + revisión cruzada por un runtime distinto |
| T3 | nuevo source/sink, salida de red o vinculación de credenciales, cambio de política, retirada | + aprobación humana |
| T4 | la constitución | solo humanos |

La constitución (L0) es el ancla definitiva. Las máquinas y los agentes pueden remodelar todo lo demás, pero solo mediante ChangeSets verificados, revisables y reversibles.
