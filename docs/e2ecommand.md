# Antigravity CLI (agy) を使ったE2E手順

```bash
# 1) 成果物リポジトリを用意（任意: Git初期化しておくと履歴管理しやすい）
mkdir -p work/CSV-AGY-01/repo
git init work/CSV-AGY-01/repo

# 2) Initiative/Phase計画＋最初のRun（Agent: antigravity）
#    Planning後そのまま最初のRunへ自動継続し、実装→検証→レビューを経て Human Check 待ちで停止。
#    プランを先に確認したい場合は --plan-only を付けると計画直後で停止する。
python3 -m dev_pipeline.cli --agent antigravity init \
  --id CSV-AGY-01 \
  --request "CSVファイルを指定すると、category列の値ごとの行数を集計し、カテゴリ名の昇順でCSV形式の標準出力に表示するコマンドラインツールを作る" \
  --constraint "Pythonで実装し、追加ライブラリなしで動くこと"

# 3) 状態確認（トークン使用量も表示）
python3 -m dev_pipeline.cli show CSV-AGY-01

# 4) Human Check（成果物を確認した後にACCEPT。FEEDBACKの場合は--feedbackを付与）
#    ACCEPTで次Phaseが存在する場合は自動的に次のPhaseが running になる。
python3 -m dev_pipeline.cli human-check CSV-AGY-01 ACCEPT

# 5) 次Phase以降のAgent Run 2Phase目以降は ACCEPT 後にこの run で進める。
python3 -m dev_pipeline.cli --agent antigravity run CSV-AGY-01 --until-ready

# 6) 最終Phaseまで 3〜5 を繰り返し、全Phase accepted で COMPLETED になる
python3 -m dev_pipeline.cli status CSV-AGY-01
```

既存リポジトリや別の場所を対象にする場合のみ、`--repo` でパスを明示します
（この場合はディレクトリを自動作成しないため、事前に用意してください）。
