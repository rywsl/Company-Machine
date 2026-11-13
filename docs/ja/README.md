[English](../en/README.md) | [简体中文](../zh-CN/README.md) | **日本語**

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

| English | 简体中文 | 日本語 |
|---|---|---|
| [README](../../README.md) | [README](../../README_zh-CN.md) | [README](../../README_ja.md) |
| [docs/en](../en/README.md) | [docs/zh-CN](../zh-CN/README.md) | [docs/ja](README.md) |
