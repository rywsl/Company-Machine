[English](../en/README.md) | **日本語** | [한국어](../ko/README.md) | [Français](../fr/README.md) | [Deutsch](../de/README.md) | [Español](../es/README.md) | [Português (BR)](../pt-BR/README.md) | [Русский](../ru/README.md) | [简体中文](../zh-CN/README.md)

# ドキュメント

<p align="center"><img src="../assets/architecture.svg" alt="アーキテクチャ" width="100%"></p>

このガイド群では、Machine Agent Org の使い方と開発への参加方法を説明します。規範となる情報源は、コントロールプレーンの [`spec/system/`](../../spec/system/README.md) と MRL 言語の [`spec/mrl/`](../../spec/mrl/) です。設計の全体的な根拠は [`docs/architecture.md`](../architecture.md)（中国語）にあります。ガイドと仕様が食い違う場合は仕様が優先します。

| ガイド | こんなときに読む |
|---|---|
| [はじめに](getting-started.md) | 10 分ほどでインストール・確認し、最初のラインを動かしたい |
| [コンセプト](concepts.md) | Bean、型、マシン、ステーション、ライン、契約、エポック、検証層を理解したい |
| [ランタイム](runtime.md) | 実行時に何が起きるかを知りたい：プロセス、job のライフサイクル、サンドボックス、ストレージ、可観測性、既知のギャップ |
| [CLI リファレンス](cli.md) | `factory` や `mrl` のコマンドを調べたい |
| [開発](development.md) | 開発に参加したい：harness のワークフロー、マイルストーン、保護ファイル、テスト |

## おすすめの読む順番

1. **新しく使う人：** はじめに → コンセプト → CLI リファレンス。
2. **工場を無人運転する運用者：** ランタイム（`serve`、可観測性、バックアップの節）→ CLI リファレンス。
3. **コントリビューター：** 開発 → コンセプト → 触るコンポーネントの仕様。

## 他の言語

English: [README](../../README.md) · [docs/en](../en/README.md) | 日本語: [README](../../README_ja.md) · [docs/ja](../ja/README.md) | 한국어: [README](../../README_ko.md) · [docs/ko](../ko/README.md) | Français: [README](../../README_fr.md) · [docs/fr](../fr/README.md) | Deutsch: [README](../../README_de.md) · [docs/de](../de/README.md) | Español: [README](../../README_es.md) · [docs/es](../es/README.md) | Português (BR): [README](../../README_pt-BR.md) · [docs/pt-BR](../pt-BR/README.md) | Русский: [README](../../README_ru.md) · [docs/ru](../ru/README.md) | 简体中文: [README](../../README_zh-CN.md) · [docs/zh-CN](../zh-CN/README.md)
