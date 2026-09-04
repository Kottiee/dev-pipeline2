# AI Development Pipeline

`spec.md` の設計を実行可能なCLIにした、Phase指向のCoding Agentハーネスです。人間の要求を
レビュー可能な複数Phaseへ分割し、Agent Run、Checkpoint、機械検証、敵対的レビュー、
Human Checkを永続的な状態機械としてつなぎます。

## 実装済みのフロー

```text
Human Request → AI Phase Planning → Phase 1 / Agent Run
  → Checkpoint → (必要なら次Run)
  → Verification → Adversarial Review → Human Check
                                            ├─ FEEDBACK → Fix Run
                                            └─ ACCEPT → Next Phase
```

* `initiative.json`、`state.json`、Phase定義を `work/<ID>/` に保存
* RunごとにMarkdown CheckpointとAgentログを保存
* 任意個のbuild/test/lintコマンドを実行し、結果とログをEvidenceとして保存
* 実装時とは独立したAgent呼び出しで、Reject理由を探す敵対的レビューを実行
* `ACCEPT` で次Phaseを自動開始し、`FEEDBACK` で同じPhaseのFix Runを開始
* stdin/stdoutの共通契約を介したCodex/OpenCodeアダプター
* JSONのatomic writeによる、Sessionに依存しない再開可能な状態管理

## 必要環境

* Python 3.11以上
* 利用するAgent CLI (`codex` または `opencode`) とその認証

インストールせずに `python -m dev_pipeline.cli` でも実行できます。CLIコマンドとして導入する場合：

```bash
python -m pip install -e .
dev-pipeline --help
```

## クイックスタート

### 開発リポジトリの置き場（運用ルール）

新規開発では `work/<ID>/repo/` を基本の置き場とします。別の場所を使いたい場合や
既存リポジトリを改修する場合は、その場所を `--repo` で指定します。
これは運用上の取り決めであり、CLIが自動で配置するものではありません。

`--repo` の省略時はカレントディレクトリが使われ、指定したパスも保存されません。
実装・検証・レビューの実行時には、再開時も含めて同じ `--repo` を明示してください。
相対パスはCLIを起動したディレクトリから解決されるため、以下の例は同じディレクトリで実行します。

### 1. 自然言語の要求から開始

Planningは人間の承認待ちで停止せず、Phase 1を即座に `running` にします。

```bash
dev-pipeline --repo . --agent codex init \
  --id INIT-001 \
  --request "チームで使える軽量なタスク管理Webアプリを作る" \
  --constraint "SQLiteで動作すること"
```

Phaseを明示して再現性のある開始をする場合は、サンプル形式のJSONを渡します。この場合のみ
Planning Agentを呼びません。

```bash
dev-pipeline init --id INIT-001 \
  --goal "タスク管理アプリを完成させる" \
  --requirement "タスクを登録・完了できる" \
  --phase-file examples/phases.json
```

### 2. Agent Runを実行

新規開発では、上記の `init` が完了してから成果物用のリポジトリを作成します。
`init` はPlanningのみで成果物を実装しません。既存の `work/<ID>/` があると `init` が失敗するため、
その配下の `repo/` を先に作成しないでください。既存リポジトリを使う場合、この作成手順は不要です。

```bash
mkdir -p work/INIT-001/repo
git init work/INIT-001/repo
```

AgentはRepositoryを直接変更し、最終出力に共通JSON契約を返します。`GOAL_REACHED` の場合は指定した
VerificationとAdversarial Reviewまで自動で進み、`CONTINUE` ならCheckpointを残して終了します。

```bash
dev-pipeline --repo work/INIT-001/repo --agent codex run INIT-001 \
  --check 'test=python -m unittest discover -v' \
  --check 'build=python -m build'
```

Human Checkまで自律的に継続する場合は `--until-ready` を指定します。`CONTINUE`、検証失敗、
レビュー指摘から次Runへ進み、Checkpoint・検証ログ・レビュー指摘を次のAgentへ渡します。
Human Checkは自動承認しません。Agentプロセス自体の起動・認証エラーは報告して終了します。

```bash
dev-pipeline --repo work/INIT-001/repo --agent codex run INIT-001 --until-ready \
  --check 'test=python -m unittest discover -v'
```

Codexの既定コマンドは `codex exec --sandbox workspace-write -` です。
認証済みCodex CLIの設定を使用し、モデルは固定しません。

AgentのCLI引数が環境と異なる場合はコマンドを差し替えられます。Promptはstdinに渡されます。

```bash
dev-pipeline --repo work/INIT-001/repo --agent codex \
  --agent-command 'codex exec --sandbox workspace-write -' run INIT-001 \
  --check 'test=pytest -q'
```

### 3. 状態確認とHuman Check

```bash
dev-pipeline status INIT-001

# 成果物を操作して問題がなければ次Phaseへ
dev-pipeline human-check INIT-001 ACCEPT

# 修正が必要なら同じPhaseを継続
dev-pipeline human-check INIT-001 FEEDBACK \
  --feedback "検索後に戻っても条件を保持してほしい"
```

検証とレビューはトラブルシュート用に個別実行もできます。

```bash
dev-pipeline --repo work/INIT-001/repo verify INIT-001 --check 'test=python -m unittest discover -v'
dev-pipeline --repo work/INIT-001/repo --agent codex review INIT-001
```

## 永続化レイアウト

```text
work/INIT-001/
├── initiative.json
├── state.json
├── repo/                         # 新規開発の成果物（運用で作成。外部配置も可）
├── phases/                       # Phase契約
├── runs/                         # Run Checkpoint
├── feedback/                     # ACCEPT / FEEDBACK
└── evidence/
    ├── phase-01-verification.json
    ├── phase-01-verification.log
    ├── phase-01-adversarial-review.json
    └── *-agent.log
```

`state.json` が現在地点のSource of Truthです。Agentプロセスが終了しても、同じ `--work-dir`・ID・`--repo` で
次のRunを開始できます。Verification失敗またはReview FindingsがあるPhaseはHuman Checkへ進みません。

## Agent出力契約

実装Runの最終JSON：

```json
{
  "result": "CONTINUE",
  "completed": ["APIを実装"],
  "remaining": ["UIを実装"],
  "problems": [],
  "recommended_next_action": "UIと統合テストを実装する"
}
```

レビューの最終JSON：

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

CLIはAgentの途中出力を許容しますが、stdout末尾は必ず単一のJSON objectである必要があります。

## 開発時の検証

```bash
python -m unittest discover -v
python -m compileall -q dev_pipeline tests
```
