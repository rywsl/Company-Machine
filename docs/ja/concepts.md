[English](../en/concepts.md) | [简体中文](../zh-CN/concepts.md) | **日本語**

# コンセプト

Machine Agent Org のメタモデルは小さくできています。動画パイプラインでも、コードレビューのラインでも、毎晩のレポートでも、作れるものはすべて以下の 7 つのプリミティブで表現します。これらは Git の registry に YAML の **Spec** として置かれ、構造コンパイラーは型チェッカーがコードを検査するのと同じように Spec を検査します。

> 設計の根拠は [`docs/architecture.md`](../architecture.md) の 3–10 節（中国語）、形式的な規則は [`spec/mrl/factory-profile.md`](../../spec/mrl/factory-profile.md) にあります。

## 1. プリミティブ

| プリミティブ | 意味 | 要点 |
|---|---|---|
| **Type** | 素材の仕様 | `name@major.minor`<br>種類：アトミック、`bundle{…}`、`list<T>`、`ref<T>`、`any`<br>payload 構成、meta スキーマ、validator（合否サンプル付き）、計測された *facts*、名前付き *properties* を持つ |
| **Bean** | 1 つの素材 | 内容アドレス（`sha256:…`）、不変、系譜と来歴付きで封印される |
| **Machine** | 既存の製品 | Machine Package で記述：digest 固定のイメージ、レシピ、入出力、probe、適合性のゴールデン |
| **Operator** | マシンを操作する主体 | エージェント CLI（Claude Code ヘッドレス、Codex）、スクリプト、受信箱経由の人 |
| **Station** | Recipe × Operator × Policy | ポートは型付き<br>役割：`source`、`transform`、`sink`。`converter`、`judge`、`planner` は transform の特化 |
| **Line** | ステーションからなる型付き DAG | さらに上限付きの動的構造を持てる。インターフェースを宣言したラインはステーションとして使える（`subline`） |
| **Policy** | 権限 | 出口ティア、シークレット、予算、承認、公開、保持期間 |

その上にメタレベルの概念が 3 つあります。
- **Spec**：上記の定義は、それぞれ `spec.<kind>@N` 型の Bean です。
- **Invariant**：MRL で書かれた憲法の規則です。
- **ChangeSet**：Spec の差分で、それ自体も Bean です。

工場自身の構造も素材なので、同じ検査で保守作業そのものも守れます。フォアマンが秩序の内側に保たれるのはこの仕組みによります。

## 2. 型、facts、properties

型は 2 つのものを宣言します。*facts* は validator が計測する値、*properties* は facts に対する名前付きの MRL 式です。

```yaml
# spec/examples/registry/types/video.yaml（抜粋）
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

各 validator には合格サンプルと不合格サンプルが付きます。不合格サンプルまで通してしまう変更（検査を「甘く」する変更）は、自動的に高リスクと判定されます。

## 3. コンシューマー契約：`guarantees` と `requires`

ポートは properties で契約を宣言します。出力ポートは**保証**する性質を列挙し、ゲートがそれを強制します。入力ポートは**要求**する性質を列挙します。

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

コンパイル時には、すべての辺が `requires ⊆ guarantees ∪ 型の properties` を満たす必要があります。これは単純な集合の包含判定なので、上流が保証を 1 つ外すと、コンパイラーは影響を受ける下流のポートを正確に示せます。実行時には、入力検査が実際の Bean で同じ properties を評価します。

## 4. 変換：成果物を自動的に素材にする

辺の型や性質が合わないとき、コンパイラーは registry から **converter** ステーションを探し、変換チェーンを 1 本だけ挿入します。候補が複数あるときは、次の順で選びます：無損失を優先 → ステップ数が最少 → 見積もりコストが最小 → 見積もり時間が最短 → 名前順。

[はじめに](getting-started.md)にその例があります。`note.raw@1 → note.text@1` を `note-to-text@1` が橋渡ししました。チェーンが見つからない場合、コンパイラーは推測しません。`E-NO-CONVERTER` を報告し、*変換要求* を出してフォアマンを起こします。フォアマンは新しいコンバーターを提案できます。

## 5. ライン

```yaml
# spec/examples/registry/lines/explainer-multilang.yaml（抜粋）
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

ラインは次の規則に従います。
- 終端の成果物は、どれかのシンクで消費されるか、`keep` に書かれていなければなりません。
- 予算は階層構造です：全体 → ライン → run → ステーション → job。
- トリガーには、Bean の到着、`cron`、`every`、手動の 4 種類があります。

### 上限付きの動的構造

| 構造 | 動作 | 上限 |
|---|---|---|
| `map` | `list<T>` の各要素でサブステップを実行 | `max_items`（超過時は拒否か切り詰め）、`max_parallel` |
| `switch` | MRL の条件で分岐を 1 つ選ぶ | 網羅的で、`default` が必須 |
| `until` | MRL の条件が成り立つまでサブステップを繰り返す | `max_iter`。使い切ると run は失敗 |
| `subline` | この run のエポック内で別のラインを実行 | 深さの上限 |
| `plan` | **planner** ステーションが `Plan`（サブ DAG）を出力 | 実行前に V1/V2 でコンパイルし、`plan_bounds` と照合 |

## 6. エポック：実行中の作業を壊さずに変更する

検証を通った registry のコミットは、どれも**エポック**になれます。run は開始時に 1 つのエポックに結び付き、終わるまでそれを使い続けます。そのため構造を変えても実行中の作業には影響しません。ロールバックは「現在のエポック」のポインターを戻すだけです。複数のエポックを同時に有効にでき、たとえば現行版とカナリアを並べて動かせます。

## 7. 6 層の検証

| 層 | 検査内容 | タイミング | 失敗時 |
|---|---|---|---|
| **V1** Spec | スキーマ、参照、バージョン固定、ポリシー lint | 提案ごと | 変更を拒否 |
| **V2** 構造 | 辺の型と契約、閉路、必須入力、成果物の行き先、権限、予算、動的構造の上限 | 提案時 + 毎回の実行前 | 変更または実行を拒否 |
| **V3** 進化 | 前バージョンとの互換性、すべての下流コンシューマー契約、影響範囲 | 提案時 | 拒否、または移行用コンバーターを要求 |
| **V4** 契約テスト | マシンの適合性、ゴールデン brief、コンバーターの往復、シンクのドライラン | 提案時 + 定期カナリア | 拒否、またはドリフト警告 |
| **V5** ランタイム | 入力検査、ゲート（構造 → validator → accept → judge）、シンクのレシート検証 | すべての job | 差し戻して再試行、または DLQ |
| **V6** 継続 | 不変条件監査、ドリフト監視、CAS と系譜の完全性、リコンサイル | 定期 + イベント駆動 | 隔離、遮断、ロールバック、フォアマンを起こす |

## 8. 4 種類のドリフト

| ドリフト | 例 | 検出する仕組み |
|---|---|---|
| 仕様ドリフト | 誰かが型やポートを編集した | V1–V3 とコンシューマー契約 |
| 振る舞いドリフト | 製品の更新、モデルやプロンプトの変更 | バージョン固定、V4 カナリア、シャドー実行 |
| 意味ドリフト | スキーマは通るが品質が落ちていく | 受け入れ式、judge の抜き取り、指標のベースライン |
| 設定ドリフト | コンテナやシークレットの割り当てを帯域外で変更 | リコンサイラーと不変条件監査 |

## 9. ガバナンスのティア

検証パイプラインは各 ChangeSet の diff からリスクティアを決定論的に算出します。誰も下げることはできず、フォアマンも例外ではありません。

| ティア | 典型的な変更 | 必要なもの |
|---|---|---|
| T0 | 再試行、隔離 | 自動 |
| T1 | テンプレート調整、コンバーター追加、型の minor 更新 | 検証 + シャドー実行 |
| T2 | マシンの導入・更新、新しいライン、型の major 更新 | + カナリア + 別ランタイムによるクロスレビュー |
| T3 | ソース/シンク追加、出口・認証情報の割り当て、ポリシー変更、廃止 | + 人の承認 |
| T4 | 憲法 | 人のみ |

憲法（L0）は最後の錨です。マシンやエージェントはそれ以外のすべてを変えられますが、変更は必ず検証済みでレビュー可能かつ元に戻せる ChangeSet を通ります。
