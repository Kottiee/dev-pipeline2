# CLI操作ガイド

このガイドは、Web UIを介さず `dev-pipeline` コマンドからパイプラインを直接操作する人向けです。コマンドはリポジトリルートで実行する前提です。

## 1. 準備

Python 3.11以上、Git、利用するAgent CLIを用意して認証します。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
dev-pipeline --help
```

インストールせずにリポジトリ内から実行する場合は、以下の `dev-pipeline` をすべて `python3 -m dev_pipeline.cli` に置き換えられます。

共通オプションはサブコマンドより前に指定します。

```text
dev-pipeline [--work-dir DIR] [--repo DIR] [--agent NAME]
             [--agent-command COMMAND] [--token-usage-format FORMAT]
             <サブコマンド> ...
```

主な共通オプション：

| オプション | 用途 | 既定値 |
|---|---|---|
| `--work-dir` | 状態、結果、ログの保存先 | `work` |
| `--repo` | Agentと検証が操作する成果物リポジトリ | `<work-dir>/<ID>/repo` |
| `--agent` | `codex` / `opencode` / `antigravity` / `command` | `codex` |
| `--agent-command` | Agentアダプターが起動するコマンドを上書き | Agentごとの既定値 |
| `--token-usage-format` | 使用量を抽出する出力形式を明示 | Agentに対応する形式 |

相対パスはコマンドを起動したディレクトリ基準です。再開時の取り違えを防ぐには、`--work-dir` と外部の `--repo` を絶対パスで固定してください。

## 2. Project + Issue 運用 (推奨)

パイプラインはリポジトリと検証ルールを共有する **Project** と、その上で順次進行する **Issue** の2層モデルを採用しています。

### プロジェクトの管理 (`dev-pipeline project`)

```bash
# プロジェクトの作成 (新規Gitリポジトリ自動初期化)
dev-pipeline project init my-service --agent codex

# 既存リポジトリを指定して作成
dev-pipeline project init existing-app --repo /path/to/repo --agent antigravity

# 共通検証チェックをプロジェクトに事前登録
dev-pipeline project init backend-api --check 'test=pytest' --check 'lint=ruff check .'

# 一覧と詳細
dev-pipeline project list
dev-pipeline project show my-service
```

### Issueの作成と実行 (`dev-pipeline issue`)

```bash
# Issueの作成と初期Run
dev-pipeline issue create my-service \
  --id 0001-login \
  --request "ログインAPIとセッション管理を実装する" \
  --requirement "POST /login でトークンを発行"

# 状態確認
dev-pipeline issue status my-service 0001-login

# 承認と次のフェーズ実行
dev-pipeline issue human-check my-service 0001-login ACCEPT
dev-pipeline issue run my-service 0001-login --until-ready

# 修正指示
dev-pipeline issue human-check my-service 0001-login FEEDBACK \
  --feedback "パスワードハッシュ化アルゴリズムをArgon2にしてください"
dev-pipeline issue run my-service 0001-login --until-ready
```

最終フェーズを承認するとIssueは `RESOLVED` になります。次のIssue（例: `0002-logout`）を開始すると、過去に解決されたIssueのゴールや受け入れ条件が自動的にプロンプトへ組み込まれます。

---

## 3. レガシー案件運用 (`dev-pipeline init`)

プロジェクト階層を使用しない単一Issueコマンド（`dev-pipeline init`）も下位互換性のため維持されています。

### 自然言語からフェーズを計画する
4. AIレビュー
5. 検証失敗・レビュー指摘がある場合の修正Run

計画結果を先に確認して実装を開始したくない場合は `--plan-only` を付けます。

```bash
dev-pipeline --agent codex init \
  --id INIT-002 \
  --request "CSV集計ツールを作る" \
  --plan-only

dev-pipeline show INIT-002
dev-pipeline --agent codex run INIT-002 --until-ready
```

### 既存リポジトリを対象にする

既存ディレクトリは自動作成されません。以後の `run`、`verify`、`review` でも同じ `--repo` を指定します。

```bash
dev-pipeline \
  --work-dir /絶対パス/pipeline-work \
  --repo /絶対パス/existing-repository \
  --agent codex \
  init --id INIT-003 --request "検索機能を追加する"
```

### フェーズをJSONで固定する

AIによる計画を省き、再現可能なフェーズ定義を渡せます。形式は `examples/phases.json` を参照してください。

```bash
dev-pipeline init \
  --id INIT-004 \
  --goal "タスク管理アプリを完成させる" \
  --requirement "タスクを登録・完了できる" \
  --phase-file examples/phases.json
```

各配列要素には `goal`、`scope`、`acceptance_criteria` が必要です。空でないJSON配列にしてください。

## 3. 機械検証を設定する

検証コマンドは次の優先順位で決まります。

1. `run --check` または `verify --check` で今回指定したコマンド
2. `init --check` で案件に登録したコマンド
3. 成果物リポジトリ直下の `devpipeline-tests.json`

Agentが用意する既定のマニフェストは次の形式です。

```json
{
  "command": "python3 -m unittest discover -v"
}
```

固定の検証を案件に登録する場合：

```bash
dev-pipeline --agent codex init \
  --id INIT-005 \
  --request "既存APIを改修する" \
  --check 'test=pytest -q' \
  --check 'lint=ruff check .'
```

一時的に差し替える場合：

```bash
dev-pipeline --agent codex run INIT-005 \
  --until-ready \
  --check 'test=pytest -q tests/unit'
```

`--check` は `NAME=COMMAND` 形式で複数指定できます。コマンドは成果物リポジトリをカレントディレクトリとしてシェル実行されます。指定も有効なマニフェストもない場合、検証はFAILになります。

機械検証を意図的に省略する場合だけ `run --no-verify` を使用します。AIレビューは引き続き実行されます。

## 4. RunからHuman Checkまで進める

`run` はAgentを1回呼びます。Agentが `CONTINUE` を返した場合はCheckpointを残して終了し、`GOAL_REACHED` を返した場合は検証とレビューも実行します。

```bash
dev-pipeline --agent codex run INIT-001
```

`CONTINUE`、検証失敗、レビュー指摘を受けて自動的に次のRunへ進み、Human Check待ちまで継続する場合：

```bash
dev-pipeline --agent codex run INIT-001 --until-ready
```

`--until-ready` はHuman Checkを自動承認しません。Agentが作業を完了できない、または検証・レビューが繰り返し失敗する場合は処理が継続するため、別のターミナルから状況とログを確認してください。

## 5. 状態と結果を確認する

```bash
# 状態機械の生JSON
dev-pipeline status INIT-001

# フェーズ一覧、現在状態、トークン・割り当て使用量の要約
dev-pipeline show INIT-001
```

詳細は `work/INIT-001/` 以下に保存されます。

- `state.json`: 現在状態
- `phases/*.json`: フェーズ目標と受け入れ条件
- `runs/*.md`: Runごとの完了事項、残作業、問題
- `evidence/*-verification.*`: 機械検証の結果とログ
- `evidence/*-adversarial-review.json`: AIレビュー結果
- `agent-logs/`: Agentへ渡したプロンプト、stdout、stderr、終了コード
- `usage.json`: 実行単位と累計の使用量

Agentログには要求やソース内容が含まれる場合があります。外部へ共有する前に確認してください。

## 6. 承認または修正依頼をする

`show` と成果物の実動作を確認し、状態が `AWAITING_HUMAN_CHECK` のときに判断します。

承認して次のフェーズへ進める場合：

```bash
dev-pipeline human-check INIT-001 ACCEPT
```

次のフェーズがあれば状態は `PHASE_RUNNING` になります。CLIでは自動実行されないため、続けて実行します。

```bash
dev-pipeline --agent codex run INIT-001 --until-ready
```

最後のフェーズなら `ACCEPT` により `COMPLETED` になります。

修正を依頼する場合：

```bash
dev-pipeline human-check INIT-001 FEEDBACK \
  --feedback "検索後に戻っても条件を保持してほしい"
dev-pipeline --agent codex run INIT-001 --until-ready
```

FEEDBACK後は同じフェーズが `PHASE_FIXING` になり、次のRunへ修正内容が渡されます。

画面など別の読み取り元で取得したrevisionに対して判断する場合は、古い状態への操作を防ぐため `--expected-revision` を指定できます。

```bash
dev-pipeline human-check INIT-001 ACCEPT \
  --expected-revision 取得済みのrevision
```

## 7. 中断後に再開する

Agent CLIやターミナルが途中で終了しても、成果物の変更と保存済み状態は残ります。状態を確認し、初回と同じ `--work-dir`、案件ID、外部リポジトリを指定して再開します。

```bash
dev-pipeline --work-dir /絶対パス/pipeline-work status INIT-003

dev-pipeline \
  --work-dir /絶対パス/pipeline-work \
  --repo /絶対パス/existing-repository \
  --agent codex \
  run INIT-003 --until-ready
```

同じ案件に対する変更コマンドはロックされ、同時実行できません。`status` と `show` は実行中でも参照できます。

## 8. 検証とレビューを個別実行する

通常は `run` が自動実行します。途中で停止した工程を診断・再開するときだけ個別コマンドを使います。

```bash
dev-pipeline --repo /絶対パス/repo verify INIT-001 \
  --check 'test=python3 -m unittest discover -v'

dev-pipeline --repo /絶対パス/repo --agent codex review INIT-001
```

`review` は機械検証に成功し、状態が `PHASE_REVIEWING` の場合だけ実行できます。

## 9. Agentを切り替える

### Codex

```bash
dev-pipeline --agent codex run INIT-001 --until-ready
```

既定コマンドは `codex exec --sandbox workspace-write --json -` です。モデルと認証はCodex CLIの設定を使います。

### OpenCode

```bash
dev-pipeline --agent opencode run INIT-001 --until-ready
```

既定では `opencode run --format json --dir <repo>` を使用します。モデルと認証はOpenCodeの設定を使います。

### Antigravity

```bash
dev-pipeline --agent antigravity run INIT-001 --until-ready
```

既定では `agy` をstream JSON形式、40分の応答タイムアウトで起動します。既知の安定性上の注意は[Antigravity実行安定性 調査レポート](antigravity-stability-report.md)を参照してください。

### 任意のコマンド

stdinでプロンプトを受け、stdout末尾に所定のJSONを返すコマンドを接続できます。

```bash
dev-pipeline \
  --agent command \
  --agent-command 'my-agent run' \
  run INIT-001 --until-ready
```

Codexなどの既定コマンドだけを差し替える場合：

```bash
dev-pipeline \
  --agent codex \
  --agent-command 'codex exec --sandbox workspace-write -' \
  run INIT-001 --until-ready
```

## 10. 主な状態

| 状態 | 意味 | 通常の次操作 |
|---|---|---|
| `PHASE_RUNNING` | 実装待ち・実装中 | `run` |
| `PHASE_VERIFYING` | 検証待ち・検証中 | `run --until-ready` または `verify` |
| `PHASE_REVIEWING` | レビュー待ち・レビュー中 | `run --until-ready` または `review` |
| `AWAITING_HUMAN_CHECK` | 人間の判断待ち | `human-check` |
| `PHASE_FIXING` | FEEDBACKを受けた修正待ち | `run` |
| `COMPLETED` | 全フェーズ承認済み | なし |

## 11. Agentの出力契約

通常は同梱プロンプトがAgentへ形式を指示するため、利用者がJSONを入力する必要はありません。独自Agentを接続するときは次の契約を維持します。

実装Run：

```json
{
  "result": "CONTINUE",
  "completed": ["APIを実装"],
  "remaining": ["UIを実装"],
  "problems": [],
  "recommended_next_action": "UIと統合テストを実装する"
}
```

`result` は `CONTINUE` または `GOAL_REACHED` です。

レビュー：

```json
{
  "result": "FINDINGS",
  "findings": [
    {
      "severity": "high",
      "description": "入力エラー時に値が失われる",
      "recommendation": "値を保持してエラーを表示する"
    }
  ]
}
```

`result` は `PASS` または `FINDINGS` です。`PASS` の場合は `findings` を空配列にします。

同梱プロンプトを差し替える場合は、`DEV_PIPELINE_PROMPTS_DIR` に `planner.txt`、`run.txt`、`review.txt` を配置します。文言は変更できますが、このJSON契約は変更できません。
