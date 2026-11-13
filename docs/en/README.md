**English** | [简体中文](../zh-CN/README.md) | [日本語](../ja/README.md)

# Documentation

<p align="center"><img src="../assets/architecture.svg" alt="Architecture" width="100%"></p>

These guides explain how to use and work on Machine Agent Org. The normative sources are the specs: [`spec/system/`](../../spec/system/README.md) for the control plane and [`spec/mrl/`](../../spec/mrl/) for the MRL language. The full design rationale is in [`docs/architecture.md`](../architecture.md), written in Chinese. When a guide and a spec disagree, the spec wins.

| Guide | Read it when you want to… |
|---|---|
| [Getting started](getting-started.md) | install, verify, and run your first line in about ten minutes |
| [Concepts](concepts.md) | understand beans, types, machines, stations, lines, contracts, epochs and the verification layers |
| [Runtime](runtime.md) | see what happens at run time: processes, job lifecycle, sandboxes, storage, observability, known gaps |
| [CLI reference](cli.md) | look up a `factory` or `mrl` command |
| [Development](development.md) | contribute: harness workflow, milestones, protected files, tests |

## Suggested reading order

1. **New users:** Getting started → Concepts → CLI reference.
2. **Operators running the factory unattended:** Runtime (sections on `serve`, observability and backup) → CLI reference.
3. **Contributors:** Development → Concepts → the spec for the component you touch.

## Other languages

| English | 简体中文 | 日本語 |
|---|---|---|
| [README](../../README.md) | [README](../../README_zh-CN.md) | [README](../../README_ja.md) |
| [docs/en](README.md) | [docs/zh-CN](../zh-CN/README.md) | [docs/ja](../ja/README.md) |
