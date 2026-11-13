[English](../en/README.md) | [日本語](../ja/README.md) | [한국어](../ko/README.md) | [Français](../fr/README.md) | [Deutsch](../de/README.md) | **Español** | [Português (BR)](../pt-BR/README.md) | [Русский](../ru/README.md) | [简体中文](../zh-CN/README.md)

# Documentación

<p align="center"><img src="../assets/architecture.svg" alt="Arquitectura" width="100%"></p>

Estas guías explican cómo usar Machine Agent Org y cómo contribuir a su desarrollo. Las fuentes normativas son las especificaciones: [`spec/system/`](../../spec/system/README.md) para el plano de control y [`spec/mrl/`](../../spec/mrl/) para el lenguaje MRL. La justificación completa del diseño está en [`docs/architecture.md`](../architecture.md), escrito en chino. Cuando una guía y una especificación no coinciden, prevalece la especificación.

| Guía | Léela cuando quieras… |
|---|---|
| [Primeros pasos](getting-started.md) | instalar, verificar y ejecutar tu primera línea en unos diez minutos |
| [Conceptos](concepts.md) | entender los beans, los tipos, las máquinas, las estaciones, las líneas, los contratos, las epochs y las capas de verificación |
| [Runtime](runtime.md) | ver qué ocurre en tiempo de ejecución: procesos, ciclo de vida de los jobs, sandboxes, almacenamiento, observabilidad, carencias conocidas |
| [Referencia de la CLI](cli.md) | consultar un comando de `factory` o `mrl` |
| [Desarrollo](development.md) | contribuir: flujo de trabajo del harness, hitos, archivos protegidos, pruebas |

## Orden de lectura recomendado

1. **Usuarios nuevos:** Primeros pasos → Conceptos → Referencia de la CLI.
2. **Quienes hacen funcionar la fábrica de forma desatendida:** Runtime (secciones sobre `serve`, observabilidad y copias de seguridad) → Referencia de la CLI.
3. **Colaboradores:** Desarrollo → Conceptos → la especificación del componente que vayas a tocar.

## Otros idiomas

English: [README](../../README.md) · [docs/en](../en/README.md) | 日本語: [README](../../README_ja.md) · [docs/ja](../ja/README.md) | 한국어: [README](../../README_ko.md) · [docs/ko](../ko/README.md) | Français: [README](../../README_fr.md) · [docs/fr](../fr/README.md) | Deutsch: [README](../../README_de.md) · [docs/de](../de/README.md) | Español: [README](../../README_es.md) · [docs/es](../es/README.md) | Português (BR): [README](../../README_pt-BR.md) · [docs/pt-BR](../pt-BR/README.md) | Русский: [README](../../README_ru.md) · [docs/ru](../ru/README.md) | 简体中文: [README](../../README_zh-CN.md) · [docs/zh-CN](../zh-CN/README.md)
