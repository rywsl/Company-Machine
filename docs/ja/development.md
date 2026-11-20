> **Migration notice.** The active control plane uses Bun + TypeScript with Podman or headless Kiri. This translated document retains historical Python milestone examples. Use the active English or Chinese guides for current commands.

# 開発

コード、`spec/`、受け入れテストは、検証結果に合わせて任意の順序で変更できます。実装より先に spec を commit する必要はありません。

## 提出前の検証

commit または push の前に次を実行します。

```bash
bun run verify:bun
```

`verify` は MRL のエラーコードと標準ライブラリ関数、factory profile、`spec/system` の章構成、第 7 章と `tests/acceptance/` の双方向対応を検査します。その後、対応する受け入れテスト、MRL conformance、MRL coverage、コードポリシーテストを実行します。対応する受け入れテストには失敗、エラー、skip があってはいけません。

## 編集の流れ

コード、spec、テストに不一致があれば、同じ変更内で修正して `verify` を再実行します。タスクの取得や開始、stage gate、タスク状態、保護ファイル lock、レビュー artifact、PreToolUse hook、Git hook は現在のワークフローにありません。`plan/tasks/`、`plan/journal/`、マイルストーンの資料は履歴として残りますが、現在の実装入口ではなく、`verify` も読みません。

## 技術ルール

依存関係を追加せず、control plane から LLM API を呼び出さず、`shell=True` を使わず、検証のためにテストを弱めません。認証情報をログ、commit、返信に出しません。無関係な変更を保持し、commit と push は明示的に依頼された場合だけ行います。
