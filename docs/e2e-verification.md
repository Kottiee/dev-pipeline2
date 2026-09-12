# LLM実機 E2E動作確認・検証指示書 (End-to-End Verification Guide)

この指示書は、**「指示書に従ってE2E動作確認を実施して」という依頼を受けた人が、1ミリも迷わずに実行・判定できる**ように設計された手順書です。

パイプラインの核となる以下のライフサイクルを検証します：
1. **プロジェクトの新規作成** (`project init`)
2. **1つ目のIssueの実行と完了** (LLM計画 → 実装・コミット → 機械検証 → AIレビュー → 人間承認 → `RESOLVED`)
3. **2つ目のIssueの追加と完了** (前Issueの成果と文脈 `$resolved_issues` をLLMが引き継いで差分実装 → 承認 → `RESOLVED`)
4. **最終成果物・Gitコミット累積の確認**

---

## ⚡ 迷ったらこれだけ：ワンコマンド完全自動実行

手動でコマンドを1つずつ叩かずに、全自動で判定まで完了させたい場合は、リポジトリ直下で以下を実行してください。

```bash
cd /Users/natto/Projects/dev-pipeline2
./scripts/run_e2e.sh
```

> **Note**:
> - デフォルトで `antigravity` を使用します。CodexやOpenCodeで実行したい場合は `E2E_AGENT=codex ./scripts/run_e2e.sh` のように環境変数を付与してください。
> - 内部で自動的に各ステップの終了コード、出力、生成コード、全単体テスト、Git履歴を検査し、最後に `ALL TESTS PASSED` とレポートします。

---

## 📖 1ステップずつ手動で進める場合の手順

ステップごとの内部動作やLLMの出力を確認しながら進める場合は、以下の通り上から順に実行してください。

### 0. 前回の作業ディレクトリのパージ・環境設定

検証をクリーンな状態で開始するため、**まず最初に前回の作業ディレクトリをパージ（初期化）**します。
（※検証終了後は生成コードやログの事後確認のために残しておき、次回実行時の最初にパージする運用とします）

端末でリポジトリ直下に移動し、パージと環境変数の設定を実行します（**そのままコピペで実行可能**です）。

```bash
cd /Users/natto/Projects/dev-pipeline2
source .venv/bin/activate

# 1. 前回の作業ディレクトリをパージ（初期化）
export E2E_WORK="/tmp/dev-pipeline-llm-e2e"
rm -rf "$E2E_WORK"

# 2. 使用エージェント (antigravity / codex / opencode から選択。デフォルト: antigravity)
export E2E_AGENT="antigravity"
```

---

### Step 1: プロジェクトの新規作成 (`project init`)

```bash
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" \
  project init calc-app \
  --agent "$E2E_AGENT" \
  --check "test=python3 -m unittest discover -v"
```

- **目安所要時間**: 約1秒
- **判定基準（Go判定）**:
  - 出力に `Project 'calc-app' initialized.` と表示されること。
  - 終了コードが `0` であること。

確認コマンド:
```bash
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" project show calc-app
```

---

### Step 2: 1つ目のIssue作成・自動実行 (`issue create`)

加算・減算機能を持つ計算モジュールと単体テストの作成を要求します。

```bash
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" \
  issue create calc-app \
  --id 0001-basic-math \
  --request "簡単な計算モジュール calc.py と単体テスト test_calc.py を作成してください。加算 add(a, b) と減算 subtract(a, b) を提供し、python3 -m unittest で全テストが通るようにしてください。"
```

> **⚠️ 注意（迷い防止）**:
> コマンド実行後、**LLMが計画・実装・検証・AIレビューを行うため、1〜3分程度ターミナルが無音（出力停止）になりますが正常です**。フリーズではないため、プロセスを止めずに `READY` が出るまでお待ちください。

- **目安所要時間**: 1〜3分
- **判定基準（Go判定）**:
  - 画面の最後の行に `READY` と表示されてプロンプトに戻ること。
  - ステータスが `AWAITING_HUMAN_CHECK` であること。

確認コマンド:
```bash
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" issue status calc-app 0001-basic-math
```
（出力 JSON 内の `"state": "AWAITING_HUMAN_CHECK"` を確認）

LLMが生成したコードの確認:
```bash
cat "$E2E_WORK/calc-app/repo/calc.py"
```

---

### Step 3: 1つ目のIssueを承認して完了させる (`human-check ACCEPT`)

生成されたコードを確認後、承認を実行します。

```bash
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" \
  issue human-check calc-app 0001-basic-math ACCEPT
```

- **目安所要時間**: 1秒以内
- **判定基準（Go判定）**:
  - エラーなく終了すること。
  - ステータスが `RESOLVED`（解決済み）になっていること。

確認コマンド:
```bash
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" issue status calc-app 0001-basic-math
```
（出力 JSON 内の `"state": "RESOLVED"`、`"current_phase": null` を確認）

---

### Step 4: 2つ目のIssueを追加する (`issue create`)

1つ目のIssueが完了した同じリポジトリに対して、乗算機能の追加を要求します。

> **🌟 最重要検証ポイント**:
> - 解決済みの `0001-basic-math` の文脈（`$resolved_issues`）が自動的にLLMへ渡されます。
> - LLMは**既存の `calc.py`（add, subtract）を壊さずに、`multiply` を追記**します。

```bash
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" \
  issue create calc-app \
  --id 0002-multiply \
  --request "既存の calc.py に乗算 multiply(a, b) 関数を追加してください。既存のテストを壊さずに test_calc.py に乗算のテストも追加してください。"
```

- **目安所要時間**: 1〜3分
- **判定基準（Go判定）**:
  - 画面の最後に `READY` と表示されること。
  - ステータスが `AWAITING_HUMAN_CHECK` であること。

コードの差分確認（`add`, `subtract` が残り、`multiply` が追加されていること）:
```bash
cat "$E2E_WORK/calc-app/repo/calc.py"
```

---

### Step 5: 2つ目のIssueを承認する (`human-check ACCEPT`)

```bash
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" \
  issue human-check calc-app 0002-multiply ACCEPT
```

- **判定基準（Go判定）**:
  - 2つ目のIssueも `"state": "RESOLVED"` になること。

確認コマンド:
```bash
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" issue status calc-app 0002-multiply
```

---

### Step 6: プロジェクト全体の最終検証（合否確定）

以下の3つのコマンドを実行して、最終合格判定を行います。

```bash
# 1. 両方のIssueが RESOLVED であること
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" issue list calc-app

# 2. 全単体テストが通ること
(cd "$E2E_WORK/calc-app/repo" && python3 -m unittest discover -v)

# 3. 生成ファイルおよびGitリポジトリの状態確認
git -C "$E2E_WORK/calc-app/repo" status --short
```

**期待される最終出力**:

`issue list`:
```text
0001-basic-math	RESOLVED	簡単な計算モジュール calc.py と単体テスト test_calc.py を作成してください...
0002-multiply	RESOLVED	既存の calc.py に乗算 multiply(a, b) 関数を追加してください...
```

`unittest discover -v`:
```text
test_add (test_calc.TestCalc...) ... ok
test_subtract (test_calc.TestCalc...) ... ok
test_multiply (test_calc.TestCalc...) ... ok

----------------------------------------------------------------------
Ran 3 tests in 0.001s

OK
```

**上記が全てOKであれば、E2E検証は完全合格です！**

---

## ❓ 迷いやすいポイントとFAQ

### Q1. コマンド実行中に画面が止まった。フリーズした？
**A.** フリーズではありません。LLMが思考中、コード生成中、AIレビュー中の間は標準出力が一時的にバッファされるため、1〜3分程度出力が止まります。
リアルタイムで進行状況を確認したい場合は、別ターミナルを開いて最新のログを監視できます：
```bash
tail -f /tmp/dev-pipeline-llm-e2e/calc-app/issues/*/agent-logs/*.json
```

### Q2. `issue create` が `READY` ではなくエラーで終了した場合は？
**A.** LLMのコードに構文エラーやテスト失敗があった場合、パイプラインは自動修復を行いますが、万が一途中で停止した場合はステータスを確認してください：
```bash
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" issue status calc-app <issue_id>
```
再開コマンドを実行することで、続きから再実行できます：
```bash
python3 -m dev_pipeline.cli --work-dir "$E2E_WORK" issue run calc-app <issue_id> --until-ready
```

### Q3. テスト後の作業ディレクトリはどう片付ければいい？
**A.** **テスト終了後の手動削除（パージ）は不要です。**
生成された成果物やログを事後確認できるよう、そのまま残しておいて問題ありません。作業ディレクトリのパージは次回検証の開始時（Step 0）に自動的に行われます。
