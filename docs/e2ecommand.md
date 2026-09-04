
```
# 1) 成果物リポジトリ用意
mkdir -p work/CSV-OPENCODE-03/repo
git init work/CSV-OPENCODE-03/repo

# 2) Initiative/Phase計画（Agent: opencode）
python3 -m dev_pipeline.cli --repo work/CSV-OPENCODE-03/repo --agent opencode init \
  --id CSV-OPENCODE-03 \
  --request "CSVファイルを指定すると、category列の値ごとの行数を集計し、カテゴリ名の昇順でCSV形式の標準出力に表示するコマンドラインツールを作る" \
  --constraint "Pythonで実装し、追加ライブラリなしで動くこと"

# 3) Agent Run（実装→検証→レビューまで自動継続）
python3 -m dev_pipeline.cli --repo work/CSV-OPENCODE-03/repo --agent opencode run CSV-OPENCODE-03 \
  --until-ready \
  --check 'test=python3 -m unittest discover -s tests -v' \
  --check 'build=python3 -m compileall -q .'

# 4) 状態確認（トークン使用量も表示）
python3 -m dev_pipeline.cli show CSV-OPENCODE-03

# 5) Human Check
python3 -m dev_pipeline.cli --repo work/CSV-OPENCODE-03/repo --agent opencode human-check CSV-OPENCODE-03 ACCEPT
```