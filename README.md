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

AgentはRepositoryを直接変更し、最終行に共通JSON契約を返します。`GOAL_REACHED` の場合は指定した
VerificationとAdversarial Reviewまで自動で進み、`CONTINUE` ならCheckpointを残して終了します。

```bash
dev-pipeline --repo . --agent codex run INIT-001 \
  --check 'test=python -m unittest discover -v' \
  --check 'build=python -m build'
```

AgentのCLI引数が環境と異なる場合はコマンドを差し替えられます。Promptはstdinに渡されます。

```bash
dev-pipeline --agent codex --agent-command 'codex exec -' run INIT-001 \
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
dev-pipeline --repo . verify INIT-001 --check 'test=python -m unittest discover -v'
dev-pipeline --repo . --agent opencode review INIT-001
```

## 永続化レイアウト

```text
work/INIT-001/
├── initiative.json
├── state.json
├── phases/                       # Phase契約
├── runs/                         # Run Checkpoint
├── feedback/                     # ACCEPT / FEEDBACK
└── evidence/
    ├── phase-01-verification.json
    ├── phase-01-verification.log
    ├── phase-01-adversarial-review.json
    └── *-agent.log
```

`state.json` が現在地点のSource of Truthです。Agentプロセスが終了しても、同じ `--work-dir` とIDで
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
