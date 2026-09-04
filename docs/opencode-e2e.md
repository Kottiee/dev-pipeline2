# OpenCode実機検証（2026-09-04）

OpenCode 1.18.25、設定済みモデル `deepseek/deepseek-v4-flash` で検証。
CSV集計リクエストはテスト入力としてのみ使用し、ハーネスにCSV専用処理は追加していない。

## 安定化の方針

- CLIの表示テキストではなく `--format json` のイベントを読み、最終回答だけを採用する。
- `--dir`、プロセスのcwd、環境変数PWDを開発対象の絶対パスに揃える。
- 不正JSONは同じセッションで構文の修正だけを求め、実装工程を最初からやり直さない。
- 権限拒否時は拒否を維持し、許可範囲の代替操作を促す。権限の一括許可や設定変更はしない。
- 復帰は合計2回まで。API・プロセスエラーは再試行せず、失敗ログと再開可能な状態を残す。
- 起動ごとのコマンド・Prompt・stdout・stderr・終了コードを `work/<ID>/agent-logs/` に保存する。

`--format json` はモデル回答自体のJSON構文を保証しないため、イベント解析と
回答の構文復帰は別に扱う。モデルや環境に依存するすべての失敗を自動解決する保証ではない。

## 再現コマンド

未使用のIDで、リポジトリのルートから実行する。

```bash
mkdir -p work/CSV-OPENCODE-STABLE/repo
git init work/CSV-OPENCODE-STABLE/repo
python3 -m dev_pipeline.cli --repo work/CSV-OPENCODE-STABLE/repo --agent opencode init \
  --id CSV-OPENCODE-STABLE \
  --request '{"title":"CSVのカテゴリ別件数集計","goal":"利用者がCSVファイルを指定し、カテゴリごとの行数をコマンドラインで確認できるようにする","success_conditions":["category列を持つCSVを入力すると、カテゴリごとの行数を標準出力にCSV形式で出力する","出力はカテゴリ名の昇順で並ぶ","category列がない場合は、標準エラーに理由を表示し、非ゼロで終了する"],"constraints":[]}'
python3 -m dev_pipeline.cli --repo work/CSV-OPENCODE-STABLE/repo --agent opencode run \
  CSV-OPENCODE-STABLE --until-ready \
  --check 'test=python3 -m unittest discover -s tests -v' \
  --check 'build=python3 -m compileall -q .'

# Human Checkで成果物を確認した後に入力する。自動承認ではない。
python3 -m dev_pipeline.cli human-check CSV-OPENCODE-STABLE ACCEPT
python3 -m dev_pipeline.cli status CSV-OPENCODE-STABLE
```

## 停止済みE2Eからの復帰で確認したこと

`CSV-OPENCODE-E2E` を `PHASE_REVIEWING` から再開した。

- レビューの権限拒否から同じセッションで復帰し、FINDINGSを取得。
- 指摘を引き継いだRun 2でBOMと不正CSV処理を修正。
- Run 2でも権限拒否から復帰し、その後の不正JSONは構文修正要求で正常に取得。
- テスト16件・compileall・再レビューがPASS。独立したCLI実行でも成功条件3件を確認。
- ACCEPTをテスト入力し `COMPLETED` へ到達。

実機の復帰ログ: `work/CSV-OPENCODE-E2E/agent-logs/` 内の
`f8f90cc646f0484594bc87a7d9c6daec-{1,2}.json`（レビュー権限拒否→復帰）、
`d4832962389d41ca82c83c4eb7fe2195-{1,2,3}.json`（実装権限拒否→不正JSON→復帰）。
ログと成果物はローカルの `work/` に残し、Gitには含めない。

## 新規E2Eの結果

`CSV-OPENCODE-STABLE` は新規リポジトリでPlanningから開始した。

- 1 Phase、2実装Run。初回レビューの指摘を受け、BOM・空行・デコードエラー処理を修正。
- 最終Verification: テスト17件・compileallがPASS。
- 独立レビュー: PASS、findingsなし。
- 独立したCLI確認: 引用符付きカテゴリ・日本語カテゴリを含む入力で件数と昇順を確認。
  category列なしではstdoutが空、理由がstderrに出力され、終了コード1。
- Human CheckのACCEPTをテスト入力し、最終状態 `COMPLETED` を確認。
- ハーネスの回帰テスト29件、compileall、git diff --checkも成功。

最終証跡は `work/CSV-OPENCODE-STABLE/state.json`、`evidence/`、`agent-logs/` に保存。
復帰経路は実機に加え、APIエラー、イベント破損、復帰上限、既存のCodex／カスタムコマンドとの
互換性、指摘付きPASSの拒否を回帰テストで確認している。
