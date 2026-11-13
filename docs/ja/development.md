[English](../en/development.md) | **日本語** | [한국어](../ko/development.md) | [Français](../fr/development.md) | [Deutsch](../de/development.md) | [Español](../es/development.md) | [Português (BR)](../pt-BR/development.md) | [Русский](../ru/development.md) | [简体中文](../zh-CN/development.md)

# 開発

Machine Agent Org は、**設計者 / 実装者**の明確な分担で開発しています。決定論的な harness（`tools/harness.py`）がこの分担を強制するので、どちらの側もこっそり合格基準を動かすことはできません。

## 1. 役割

| 役割 | 担当 | できないこと |
|---|---|---|
| **設計者** | 仕様（`spec/`）、計画とタスクカード（`plan/`）、受け入れテスト（`tests/acceptance/`）、レビュー | 本番コードの実装 |
| **実装者** | `src/`、システムテスト（`tests/system/`）、作業ログ | 仕様・受け入れテスト・合格基準の変更、インターフェースの発明、後のマイルストーンの先取り |
| **人** | 憲法、ADR、**保護ファイルの再登録**（`harness lock --update`） | — |

保護ファイルは `plan/protected.lock` に glob で登録され、SHA-256 で固定されています。
- PreToolUse フック（`.claude/settings.json`）が、エージェントによる編集を拒否します。
- `harness lock --verify` が、あらゆる変更を検出します。

実装者は矛盾を見つけたら作業を止め、`plan/journal/` に記録して `harness task block` を実行します。設計者が保護ファイルを直し、人がロックを再登録します。

## 2. harness のループ

```bash
uv run python tools/harness.py doctor            # 環境
uv run python tools/harness.py status            # マイルストーン、タスク、ゲート
uv run python tools/harness.py task next         # 依存が完了している最初のタスク
uv run python tools/harness.py task show M9-01   # タスクカードと必読の章
uv run python tools/harness.py task start M9-01
# … 実装 …
uv run python tools/harness.py accept M9-01      # タスクの受け入れ検査を実行し、証拠を記録
uv run python tools/harness.py gate M9           # ステージゲート：マイルストーンの全検査（継承分を含む）
```

その他のサブコマンド：
- `doccheck`：仕様の第 7 章のテスト表とテスト関数が一致しているか検査します。
- `check`：単一の検査を実行します。
- `scenario`：`plan/scenarios/` のスクリプト化されたシナリオを実行します。
- `pytest`：pytest を実行して JSON の要約を出力します。`tests` パッケージのインポートパスを設定するので、素の `pytest` ではなく必ずこちらを使ってください。
- `lock --verify`：保護ファイルをロックと照合します。

証拠（コマンド、終了コード、出力のダイジェスト、git コミット）は `plan/state.json` に書かれます。

## 3. マイルストーン

```mermaid
flowchart LR
    M0["M0<br/>構造コンパイラー"] --> M1["M1<br/>単一ステーション"] --> M2["M2<br/>ラインと外部世界"] --> M3["M3<br/>ガバナンスとフォアマン"] --> M4["M4<br/>動的構造と本番化"]
    M4 --> M5["M5<br/>自律ループ"] --> M6["M6<br/>実際のエージェント CLI"] --> M7["M7<br/>運用"] --> M8["M8<br/>レビュー修正"] --> M9["M9<br/>クロージャー修正"]
```

各マイルストーンの終わりには、システムは使える状態で、引き継げる状態になっています。各ステージゲートは、1 つ前のゲートの検査をすべて引き継ぎます。

M5、M8、M9 は**是正**のマイルストーンで、それぞれ設計者のレビュー（`plan/drift.md`、`plan/review-m5-m7.md`、`plan/review-closure.md`）から生まれました。レビューでは、自分のテストには合格するのに本番経路につながっていない機能が見つかりました。そこから得た教訓は、受け入れテストを書く際の規則になっています：**本番のエントリーポイントを駆動し、テストダブルには振る舞いを検証させること。** たとえば偽の S3 は SigV4 を再計算し、偽の `psql` はすべての呼び出しを記録し、偽の `factoryctl` は実際のメソッドとパスを転送します。

## 4. テスト

| 層 | 場所 | 内容 |
|---|---|---|
| 受け入れ | `tests/acceptance/m0…m9/` | タスクごとに 1 ファイル。仕様の第 7 章に列挙され、保護されている |
| システム | `tests/system/` | 実装者が持つ単体・結合テスト |
| 適合性 | `spec/mrl/conformance/`、`tests/test_conformance.py` | MRL の式・ルール・factory のケース。既存ケースは追加のみ |
| ポリシー | `tests/test_codebase_policy.py`、`tests/test_hooks.py` | `src/mao` に対する静的規則（インポートしたオブジェクトへのパッチ禁止、カレントディレクトリの参照禁止、他モジュールの private メンバー参照禁止など） |

テストダブルは `tests/fakes/` にあります。
- `fake_agent.py`：スクリプト化された Claude Code のそっくりさんで、振る舞いはタスク Bean の `task.json` で決まります。
- `fake_llm.py`：ライブ層で使うモデルの上流です。
- `tools/`：フィクスチャ用のツールです。

フィクスチャ registry は `tests/fixtures/registry-m1` と `registry-m2` です。Docker 層の前に `uv run python tools/build_test_images.py` を実行してください。Docker がないとこれらはスキップされますが、`MAO_REQUIRE_DOCKER=1` を設定すると失敗扱いになります。ステージゲートはスキップゼロを要求します。

## 5. コードの規則

以下は `spec/system/README.md`（決定事項 D1–D14）と `plan/README.md` 第 6 節に基づく規則です。

- Python ≥ 3.12。ランタイム依存は標準ライブラリ、`pyyaml`、`jsonschema`、リポジトリ内の `mrl` のみで、依存を増やすには ADR が要ります。Docker はサブプロセスで CLI を呼び、SQLite は `sqlite3` を直接使います。
- `shell=True` は禁止。LLM API の呼び出しも禁止で、知能はオペレーター CLI からのみ得ます。
- **MRL が唯一の式・テンプレート言語です。** Spec のテンプレートは `mrl.expr` でコンパイルし、Spec の内容に `eval`、Jinja、`str.format` を使うことは禁止です。
- ライブラリコードのパスは `Context.root` か呼び出し元の引数から取り、カレントディレクトリは参照しません。
- 実行時の SQL は SQLite と PostgreSQL の間で移植可能でなければなりません。SQLite 固有の書き方は `db.py`、`backends.py`、`backup.py` でのみ許されます。
- `uv run ruff check src tests tools` と `uv run ruff format --check` に通ること。行の長さは 110 です。

## 6. 次に読むもの

| トピック | ドキュメント |
|---|---|
| ワークフローの規則、作業ログの形式、禁止事項 | [`plan/README.md`](../../plan/README.md) |
| マイルストーンと終了条件 | [`plan/milestones.md`](../../plan/milestones.md)、[`plan/gates.yaml`](../../plan/gates.yaml) |
| コンポーネント横断の決定と規約 | [`spec/system/README.md`](../../spec/system/README.md) |
| MRL 言語 | [`spec/mrl/mrl-1.0.md`](../../spec/mrl/mrl-1.0.md)、[`spec/mrl/factory-profile.md`](../../spec/mrl/factory-profile.md) |
| 実装者向けの指示 | [`AGENTS.md`](../../AGENTS.md) |
