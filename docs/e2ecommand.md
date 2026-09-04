# Antigravity CLI (agy) を使ったE2E手順

`--repo` を省略すると `work/<ID>/repo` へ自動解決される

```bash
# 1) 成果物リポジトリを用意（任意: Git初期化しておくと履歴管理しやすい）
mkdir -p work/CSV-AGY-01/repo
git init work/CSV-AGY-01/repo

# 2) Initiative/Phase計画（Agent: antigravity）
#    work/CSV-AGY-01/repo を対象にPlanningし、Phase-01をrunningにする。
python3 -m dev_pipeline.cli --agent antigravity init \
  --id CSV-AGY-01 \
  --request "CSVファイルを指定すると、category列の値ごとの行数を集計し、カテゴリ名の昇順でCSV形式の標準出力に表示するコマンドラインツールを作る" \
  --constraint "Pythonで実装し、追加ライブラリなしで動くこと"

# 3) Agent Run（実装→検証→レビューまで自動継続）
#    実装エージェントが自動テストと devpipeline-tests.json（実行コマンド宣言）を書き、
#    検証がそのマニフェストのコマンドを実行する。指定は不要。
#    独自検証を使うなら init の --check、または run の --check で差し替え。--no-verify でスキップ可。
python3 -m dev_pipeline.cli --agent antigravity run CSV-AGY-01 --until-ready

# 4) 状態確認（トークン使用量も表示）
python3 -m dev_pipeline.cli show CSV-AGY-01

# 5) Human Check（成果物を確認した後にACCEPT。FEEDBACKの場合は--feedbackを付与）
python3 -m dev_pipeline.cli human-check CSV-AGY-01 ACCEPT
```

既存リポジトリや別の場所を対象にする場合のみ、`--repo` でパスを明示します
（この場合はディレクトリを自動作成しないため、事前に用意してください）。
