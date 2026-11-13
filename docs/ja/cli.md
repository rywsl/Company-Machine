[English](../en/cli.md) | **日本語** | [한국어](../ko/cli.md) | [Français](../fr/cli.md) | [Deutsch](../de/cli.md) | [Español](../es/cli.md) | [Português (BR)](../pt-BR/cli.md) | [Русский](../ru/cli.md) | [简体中文](../zh-CN/cli.md)

# CLI リファレンス

`uv sync` で 2 つのエントリーポイントがインストールされます。

| コマンド | パッケージ | 用途 |
|---|---|---|
| `factory` | `mao.cli:main` | 工場のコントロールプレーン |
| `mrl` | `mrl.cli:main` | MRL（MAO Relation Language）ツールキット：評価、検査、診断コードの説明 |

サンドボックス内では、オペレーターが 3 つ目のツール `factoryctl` を使います（[ランタイム § 3](runtime.md#3-ステーションの内部) を参照）。

どのコマンドも `--help` を付ければ正確な引数がわかります。規範となるコマンド一覧は [`spec/system/commands.md`](../../spec/system/commands.md) です。

## グローバルオプション

| オプション | 環境変数 | 既定値 | 意味 |
|---|---|---|---|
| `--root DIR` | `FACTORY_ROOT` | `.factory` | 工場の状態ディレクトリ |
| `--registry DIR` | `FACTORY_REGISTRY` | `registry` | registry（Spec）のディレクトリ |
| `--provider docker\|local` | | `docker` | サンドボックスプロバイダー |
| `--local-commands FILE` | | | ローカルプロバイダー用の コマンド名 → ホストのコマンド 対応表 |
| `--epoch COMMIT` | | 現在のエポック | 特定のエポックで実行 |
| `--json` | | | 機械可読な出力 |
| `--version` | | | バージョンを表示 |

## 終了コード

| コード | 意味 |
|---|---|
| 0 | 成功 |
| 1 | 業務上の失敗：job が `DEAD`、run が `FAILED`/`CANCELLED`、契約テストが `fail`、監査に未解決の指摘あり |
| 2 | 使い方の誤り |
| 3 | registry が不正（`REGISTRY_INVALID`） |
| 4 | 環境・設備のエラー（Docker が使えないなど） |
| 5 | 内部エラー |

## 構造とガバナンス

| コマンド | 説明 |
|---|---|
| `check ROOT` | registry を読み込んでコンパイル。エラーがあれば終了コード 3 |
| `epoch init COMMIT` · `ls` · `show [COMMIT]` · `rollback [COMMIT]` · `adopt COMMIT` | エポックと現在のポインターを管理 |
| `changeset propose FILE --commit C --by ACTOR` · `ls [--state S]` · `show ID` · `withdraw ID --by ACTOR` · `advance ID` | ChangeSet パイプライン |
| `governance tick` · `governance safe-mode` | ChangeSet を 1 回進める。セーフモードを表示・評価 |
| `contract validator …` · `golden …` · `equivalence …` · `diff …` · `sink-dry-run …` · `ls` | V4 契約テスト |
| `audit once` · `audit ls [--state S]` | 不変条件監査の実行 / 指摘の一覧 |
| `drift ls` | ドリフトの指摘 |

## 素材

| コマンド | 説明 |
|---|---|
| `bean import --type TYPE --file PATH=HOST …` | 外部ファイルを Bean として受け入れ、その id を表示 |
| `bean lineage BEAN --direction up\|down` | 上流または下流の job と Bean |
| `bean label BEAN KEY=VALUE` | ラベルを付ける（ラベルでラインを起動できる） |
| `cache ls [--station S@N]` · `explain JOB` · `invalidate (--key\|--job\|--station\|--bean) … --reason TEXT` | job キャッシュ |
| `gc [--dry-run]` | 保持期間：トゥームストーン、blob の掃除、作業領域の回収 |

## 実行

| コマンド | 説明 |
|---|---|
| `job run STATION --input PORT=BEAN …` | job を 1 つ投入してその場で実行（封印なら 0、DEAD なら 1） |
| `job show JOB` · `job exec JOB` | job を確認。投入済みの job のリースを取って実行（serve のワーカー子プロセスが使う） |
| `line plan LINE@N` | ラインを実行計画（ノード、コンバーター、予算）にコンパイル |
| `line run LINE@N --in PORT=BEAN … [--budget USD] [--variant STEP=V] [--no-cache]` | run を開始し、完了まで進める |
| `line ls` · `show RUN` · `nodes RUN` · `cancel RUN` · `retry RUN` · `rebuild RUN` | run の確認と制御 |
| `trigger process` | ラインのトリガーを 1 回評価 |
| `schedule tick` · `dlq ls` · `quarantine BEAN --reason TEXT` · `release BEAN` | スケジューラー、デッドレター、隔離 |
| `approval ls [--job J] [--status S]` · `show ID` · `decide ID approve\|deny --by NAME [--note TEXT]` | 承認ポイント |

## 接続

| コマンド | 説明 |
|---|---|
| `image build …` · `image ls` | マシンイメージのビルドと一覧（フォアマンが書いた Dockerfile） |
| `gateway manifest MACHINE@V` | 形態 B のマシン用サービスゲートウェイのマニフェスト |
| `egress check STATION@N` | ステーションに実際に適用される出口計画を表示 |
| `secret ls` | シークレットの名前と分類（値は決して表示しない） |
| `backend status` · `migrate …` · `verify` | ストレージバックエンド：ローカル ↔ S3 + Postgres |
| `backup create` · `verify` · `restore` | 状態と blob のバックアップ |

## 自律

| コマンド | 説明 |
|---|---|
| `serve [--once]` | デーモン：トリガー、run、job、ガバナンス、監査、カナリア、ウェイクチケット |
| `foreman next` · `done WAKE` · `get KEY` · `list KIND` · `diff BASE COMMIT` · `bean …` · `job …` · `dlq` · `drift` · `invariant` · `metrics` · `approval` · `changeset` · `review` · `conversion` · `supervise` · `sessions` · `wakes` · `reopen` | フォアマンの操作面 |
| `mcp --role foreman\|reviewer` | 同じ操作面を stdio 上の MCP サーバーとして提供 |
| `maintain bootstrap` | 保守用ラインと `registry-sink` が揃っているか確認。欠けていれば終了コード 1 で一覧を表示 |

## 可観測性

| コマンド | 説明 |
|---|---|
| `status` | 工場全体の JSON スナップショット |
| `metrics` | Prometheus テキスト形式 |
| `events [--since SEQ] [--kind K] [--limit N]` | イベントログ |
| `console [--port N]` | ローカルの読み取り専用 Web コンソール（既定 `127.0.0.1:8090`） |

## `mrl`

| コマンド | 説明 |
|---|---|
| `mrl eval EXPR` · `mrl template TEXT` | 式を評価 / テンプレートを展開 |
| `mrl check MODULE` · `mrl run MODULE` | ルールモジュールの静的検査 / 評価 |
| `mrl registry …` | registry の検査と照会（`mrl registry check --strict`） |
| `mrl resolve …` | 仮想の辺について変換を解決 |
| `mrl explain CODE` | `E-NO-CONVERTER` などの診断コードを説明 |
| `mrl conformance` · `mrl coverage` | 適合性ケースの実行 / factory ケースのカバレッジ表示 |
