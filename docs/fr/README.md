[English](../en/README.md) | [日本語](../ja/README.md) | [한국어](../ko/README.md) | **Français** | [Deutsch](../de/README.md) | [Español](../es/README.md) | [Português (BR)](../pt-BR/README.md) | [Русский](../ru/README.md) | [简体中文](../zh-CN/README.md)

# Documentation

<p align="center"><img src="../assets/architecture.svg" alt="Architecture" width="100%"></p>

Ces guides expliquent comment utiliser Machine Agent Org et comment contribuer à son développement. Les sources normatives sont les specs : [`spec/system/`](../../spec/system/README.md) pour le plan de contrôle et [`spec/mrl/`](../../spec/mrl/) pour le langage MRL. La justification complète de la conception se trouve dans [`docs/architecture.md`](../architecture.md), rédigé en chinois. En cas de désaccord entre un guide et une spec, c'est la spec qui fait foi.

| Guide | À lire si vous voulez… |
|---|---|
| [Prise en main](getting-started.md) | installer, vérifier et exécuter votre première ligne en une dizaine de minutes |
| [Concepts](concepts.md) | comprendre les beans, les types, les machines, les stations, les lignes, les contrats, les epochs et les couches de vérification |
| [Runtime](runtime.md) | voir ce qui se passe à l'exécution : processus, cycle de vie des jobs, sandboxes, stockage, observabilité, lacunes connues |
| [Référence de la CLI](cli.md) | trouver une commande `factory` ou `mrl` |
| [Développement](development.md) | contribuer : workflow du harness, jalons, fichiers protégés, tests |

## Ordre de lecture conseillé

1. **Nouveaux utilisateurs :** Prise en main → Concepts → Référence de la CLI.
2. **Exploitants qui font tourner l'usine sans surveillance :** Runtime (sections sur `serve`, l'observabilité et la sauvegarde) → Référence de la CLI.
3. **Contributeurs :** Développement → Concepts → la spec du composant que vous modifiez.

## Autres langues

English: [README](../../README.md) · [docs/en](../en/README.md) | 日本語: [README](../../README_ja.md) · [docs/ja](../ja/README.md) | 한국어: [README](../../README_ko.md) · [docs/ko](../ko/README.md) | Français: [README](../../README_fr.md) · [docs/fr](../fr/README.md) | Deutsch: [README](../../README_de.md) · [docs/de](../de/README.md) | Español: [README](../../README_es.md) · [docs/es](../es/README.md) | Português (BR): [README](../../README_pt-BR.md) · [docs/pt-BR](../pt-BR/README.md) | Русский: [README](../../README_ru.md) · [docs/ru](../ru/README.md) | 简体中文: [README](../../README_zh-CN.md) · [docs/zh-CN](../zh-CN/README.md)
