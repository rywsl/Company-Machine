[English](../en/README.md) | [日本語](../ja/README.md) | [한국어](../ko/README.md) | [Français](../fr/README.md) | [Deutsch](../de/README.md) | [Español](../es/README.md) | [Português (BR)](../pt-BR/README.md) | [Русский](../ru/README.md) | **简体中文**

# 文档

<p align="center"><img src="../assets/architecture.svg" alt="架构" width="100%"></p>

这些指南说明怎样使用和开发 Machine Agent Org。规范性来源是 [`spec/system/`](../../spec/system/README.md)（控制面）和 [`spec/mrl/`](../../spec/mrl/)（MRL 语言）。完整的设计理由见 [`docs/architecture.md`](../architecture.md)。指南与规范不一致时，以规范为准。

| 指南 | 适合在你想…… 的时候读 |
|---|---|
| [快速上手](getting-started.md) | 花大约十分钟完成安装、验证，并跑通第一条产线 |
| [核心概念](concepts.md) | 理解 Bean、类型、机器、工位、产线、契约、epoch 和各层验证 |
| [运行时](runtime.md) | 了解运行期发生了什么：进程、job 生命周期、沙箱、存储、可观测性、已知缺口 |
| [命令行参考](cli.md) | 查找 `factory` 或 `mrl` 的某条命令 |
| [开发流程](development.md) | 参与开发：harness 工作流、里程碑、受保护文件、测试 |

## 建议阅读顺序

1. **新用户：** 快速上手 → 核心概念 → 命令行参考。
2. **无人值守运行工厂的运维者：** 运行时（`serve`、可观测性、备份几节）→ 命令行参考。
3. **贡献者：** 开发流程 → 核心概念 → 你要改的组件的规范。

## 其他语言

English: [README](../../README.md) · [docs/en](../en/README.md) | 日本語: [README](../../README_ja.md) · [docs/ja](../ja/README.md) | 한국어: [README](../../README_ko.md) · [docs/ko](../ko/README.md) | Français: [README](../../README_fr.md) · [docs/fr](../fr/README.md) | Deutsch: [README](../../README_de.md) · [docs/de](../de/README.md) | Español: [README](../../README_es.md) · [docs/es](../es/README.md) | Português (BR): [README](../../README_pt-BR.md) · [docs/pt-BR](../pt-BR/README.md) | Русский: [README](../../README_ru.md) · [docs/ru](../ru/README.md) | 简体中文: [README](../../README_zh-CN.md) · [docs/zh-CN](../zh-CN/README.md)
