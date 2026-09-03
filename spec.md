AI Development Pipeline 設計書

1. 目的

人間が目的・要求・制約を入力すると、AIが開発を複数のPhaseへ分割し、各Phaseの設計・実装・検証を自律的に進める。

人間は実装途中の細かい判断には関与せず、Phase単位で動作可能な成果物を確認する。

人間が主に確認する観点は以下とする。

* 実際に使えるか
* 使用感に問題がないか
* 業務要件を満たしているか
* 次のPhaseへ進めてよいか

Human Checkで ACCEPT された場合は次のPhaseへ進み、FEEDBACK が返された場合は同じPhaseを修正する。

⸻

2. 設計原則

2.1 PhaseはHuman Review Unitとする

Phaseは、Agent内部の細かな実装単位ではなく、人間が一度成果物を操作して評価する単位とする。

Phaseは以下を満たす粒度を基本とする。

* 単体で意味のある成果になる
* 人間が実際に操作・評価できる
* 次のPhaseとの境界が明確である
* Agentが一定時間自律的に作業できる規模である

時間はPhaseの定義には使用しない。

ただし運用上は、通常のPhaseについて30分〜2時間程度、中心として1時間前後のAgent作業量になることを目安とする。

⸻

2.2 RunはAgent Execution Unitとする

1 Phaseを必ず1つのAgent Sessionで完了させる必要はない。

Phase内部を複数のRunに分けることを許容する。

Phase
 │
 ├─ Run 1
 │    ↓
 │  Checkpoint
 │
 ├─ Run 2
 │    ↓
 │  Checkpoint
 │
 ├─ Run 3
 │    ↓
 │
 ├─ Phase Verification
 ├─ Adversarial Review
 │
 ▼
Human Check

RunはWork Itemではない。

PipelineはRunごとの詳細な作業内容や依存関係を管理しない。

AgentはPhaseの現在状態を確認し、そのRunで何を行うかを自ら判断する。

⸻

2.3 Work ItemはPipelineの概念として持たない

Phase内部の作業分解はAgentに任せる。

Agentが内部的にTODOや実装ステップへ分解することは許容するが、Pipelineは以下のようなWork Item管理を行わない。

WI-001
WI-002
dependency
owner
status
artifact
...

Pipelineが管理する主要単位は以下のみとする。

Initiative
   ↓
Phase
   ↓
Run

⸻

2.4 Phase Planning後は停止しない

Human Requestを受け取ったAgentはInitiativeを複数Phaseへ分割する。

Phase計画について、事前のHuman Approvalは要求しない。

Human Request
      ↓
Phase Planning
      ↓
Phase 1 Execution

最初の人間確認はPhase 1の完成後に行う。

その際、人間はPhase 1の成果物だけでなく、必要であれば以降のPhase構成についてもFeedbackできる。

⸻

2.5 機械判定可能な問題では人間を呼ばない

以下のような問題はAgent側で解決する。

* Build失敗
* Test失敗
* Lint失敗
* Static Analysis失敗
* Applicationが起動しない
* Smoke Test失敗
* 明確なAcceptance Criteria未達
* Adversarial Reviewで発見された問題

基本ループは以下とする。

Implement
    ↓
Verification
    │
    ├─ FAIL ──→ Fix
    │             │
    └─────────────┘
    ↓ PASS
Adversarial Review
    │
    ├─ FINDINGS → Fix
    │
    ↓ PASS
Human Check

⸻

3. 全体フロー

flowchart TD
    A["Human Request<br/>目的・要求・制約"]
    A --> B["Initiative / Phase Planning"]
    B --> C["Phase 1 Start"]
    subgraph PHASE["Phase Execution"]
        C --> D["Agent Run"]
        D --> E["Checkpoint / Progress保存"]
        E --> F{"Phase Goal達成?"}
        F -->|"NO"| G["Next Agent Run"]
        G --> E
        F -->|"YES"| H["Phase Verification"]
        H -->|"FAIL"| G
        H -->|"PASS"| I["Adversarial Review<br/>別Agent / 別Context"]
        I -->|"FINDINGS"| G
        I -->|"PASS"| J["Phase Ready"]
    end
    J --> K{"Human Check"}
    K -->|"ACCEPT"| L["Next Phase"]
    L --> C
    K -->|"FEEDBACK"| M["Feedback保存"]
    M --> N["Phase Fix Run"]
    N --> H

⸻

4. Human Request

Pipelineの開始入力。

最低限以下を受け取る。

* Goal
* Requirements
* Constraints

必要に応じて、既存リポジトリや参考資料を追加できる。

Human Request自体の形式は、自然言語入力を許容する。

Pipeline内部では固定形式へ正規化する。

⸻

5. Initiative Planning

Human Requestから、Initiative全体のPhase構成を生成する。

各Phaseは最低限以下を持つ。

* Goal
* Scope
* Acceptance Criteria
* Status
* Sequence

Phase計画はAgentによって変更可能とする。

Phase 1の実装やHuman Feedbackによって、後続Phaseの追加・削除・統合・分割が必要と判断された場合は再計画できる。

ただし、すでに ACCEPT 済みのPhaseの事実は変更しない。

⸻

6. Phase Execution

Phase開始時、Agentは以下を読み込む。

Initiative Goal
Phase Goal
Scope
Acceptance Criteria
Current repository
Previous accepted phases
Current progress
Human Feedback（存在する場合）
Verification configuration

Agentはその情報から、そのRunで必要な作業を自ら判断する。

具体的には必要に応じて、

Understand
Plan
Implement
Test
Inspect
Fix

を行う。

これらをPipeline上の独立した状態にはしない。

Agent内部の作業として扱う。

⸻

7. Run

7.1 目的

Agent Sessionの長さやContext制限と、Human Reviewの粒度を分離するための実行単位。

Phaseが1 Runで完了してもよいし、複数Runにまたがってもよい。

⸻

7.2 Run終了時

Phaseが未完成の状態でRunが終了する場合、次Runが再開できるCheckpointを保存する。

最低限以下を残す。

* 完了した内容
* 未完了の内容
* 現在認識している問題
* 次に行うべき作業
* Verification状況

次のAgentはこの情報と現在のRepositoryを確認して作業を継続する。

⸻

7.3 Run数は事前に決めない

例えば、

Run 1 = DB
Run 2 = API
Run 3 = UI

のような固定計画をPipeline側では作らない。

Agent自身が必要に応じてRunを継続する。

⸻

8. Verification

VerificationはPhase完成判定の前提となる。

プロジェクトに応じて以下を実行する。

* Build
* Unit Test
* Integration Test
* Lint
* Static Analysis
* Application Startup
* Smoke Test
* その他のプロジェクト固有Command

Verificationが失敗した場合、Human Checkには進まない。

Agentが修正し、再度Verificationを実行する。

⸻

9. Adversarial Review

Phase Verification通過後、実装担当とは異なるAgentまたはContextでレビューを実行する。

Reviewerの役割は成果物を肯定することではなく、

PhaseをRejectすべき理由を探すこと

とする。

主な確認対象：

* Acceptance Criteria未達
* 要求の解釈ミス
* 実装漏れ
* 回帰
* テスト不足
* 境界条件
* 不自然な設計
* 実際の利用時に問題になる挙動

ReviewerがFindingを返した場合、Human Checkへは進まずPhase Executionへ差し戻す。

Verification PASS
       ↓
Adversarial Review
       │
       ├─ FINDINGS → Fix Run
       │
       └─ PASS → Human Check

⸻

10. Human Check

Human Checkは通常のPipelineにおける主要な人間介入ポイントとする。

人間には少なくとも、

Phase Goal
実行方法
確認方法
Verification結果
Adversarial Review結果
動作可能な成果物

を提示する。

人間は主として実際の成果物を操作する。

結果は以下の2つとする。

ACCEPT
FEEDBACK

ACCEPT

Phaseを完了状態とし、次Phaseを開始する。

FEEDBACK

同じPhaseを継続する。

Feedbackを保存し、新しいPhase Fix Runを開始する。

⸻

11. Phase Fix

Human Feedbackを受けても、Phaseを最初から再実行しない。

入力として以下を使用する。

Original Phase Goal
Scope
Acceptance Criteria
Current implementation
Current progress
Previous verification evidence
Human Feedback

Agentが必要な修正範囲を判断する。

Human Feedback
      ↓
Understand Feedback
      ↓
Re-plan if necessary
      ↓
Fix
      ↓
Verification
      ↓
Adversarial Review
      ↓
Human Check

FeedbackがPhaseの前提そのものを変更する場合は、Agentが必要に応じてPhase設計を再検討してよい。

⸻

12. Harness

HarnessはPipeline上の工程ではなく、Agentが安定して開発するための周辺環境とする。

主な対象は以下。

Harness
├── Agent instructions
├── Repository conventions
├── Architecture / project documentation
├── Build command
├── Test command
├── Lint / Static Analysis
├── Startup / Smoke Test
├── Checkpoint persistence
├── Verification evidence
└── Agent feedback loop

例えば、

「ちゃんとテストしてください」

というPromptだけに依存するのではなく、

project verify

のような一貫した検証手段をAgentが利用できる状態を優先する。

⸻

13. Agent非依存設計

Pipelineは特定のCoding Agentへ依存させない。

初期対象：

* Codex
* OpenCode

将来的には他Agentも追加できる構造とする。

             AI Development Pipeline
                       │
                       ▼
                Agent Interface
                       │
             ┌─────────┴─────────┐
             │                   │
           Codex              OpenCode
          Adapter              Adapter

Pipeline側では以下を共通化する。

* Phase入力
* Checkpoint
* Feedback
* Verification
* Review結果
* Agent Run結果

Agent固有のCLI、Session、Prompt、Resume方法などはAdapter側で吸収する。

⸻

14. 状態管理

LLMのContextをSource of Truthにしない。

Pipelineの現在状態は永続化する。

概念上は以下の状態を持つ。

PLANNING
PHASE_RUNNING
PHASE_VERIFYING
PHASE_REVIEWING
AWAITING_HUMAN_CHECK
PHASE_FIXING
COMPLETED
FAILED

Agent Sessionが終了しても、Repositoryと保存済み状態から再開できることを前提とする。

⸻

15. 固定する成果物

最初のバージョンでは、固定形式にする成果物を必要最小限に限定する。

推奨構成：

work/
└── INIT-001/
    ├── initiative.json
    ├── state.json
    │
    ├── phases/
    │   ├── phase-01.json
    │   ├── phase-02.json
    │   └── ...
    │
    ├── runs/
    │   └── ...
    │
    ├── feedback/
    │   └── ...
    │
    └── evidence/
        └── ...

以下の形式をPipelineの契約とする。

⸻

16. initiative.json

Initiative全体の目的とPhase構成を保存する。

{
  "id": "INIT-001",
  "goal": "ユーザーが○○できるアプリケーションを構築する",
  "requirements": [
    "要求A",
    "要求B"
  ],
  "constraints": [
    "制約A"
  ],
  "phase_order": [
    "phase-01",
    "phase-02",
    "phase-03"
  ]
}

Phase詳細は phase-XX.json に分離する。

⸻

17. phase-XX.json

Phaseの契約を保存する。

{
  "id": "phase-01",
  "sequence": 1,
  "goal": "Phase単体で達成する成果",
  "scope": [
    "対象A",
    "対象B"
  ],
  "acceptance_criteria": [
    "条件A",
    "条件B"
  ],
  "status": "running"
}

status は最低限以下を許可する。

pending
running
awaiting_human_check
accepted

実行途中の詳細をPhase定義へ詰め込まない。

⸻

18. state.json

Pipeline全体の現在地点を保存する。

{
  "initiative_id": "INIT-001",
  "state": "PHASE_RUNNING",
  "current_phase": "phase-01",
  "current_run": 3,
  "last_updated_at": "2026-09-04T03:00:00+09:00"
}

これはPipelineの現在状態に関するSource of Truthとする。

⸻

19. Run Checkpoint

Run間の引き継ぎ用。

JSONでもMarkdownでもよいが、Agentが読み書きしやすい形式を固定する。

初期実装ではMarkdownを推奨する。

# Run Checkpoint
## Phase
phase-01
## Goal
顧客登録機能を業務利用可能にする。
## Completed
- DB schemaを追加
- APIを実装
- Validationを追加
## Remaining
- UI実装
- Integration Test
- Smoke Test
## Current Problems
- なし
## Verification
- Build: PASS
- Unit Test: PASS
- Integration Test: NOT RUN
## Recommended Next Action
UIを実装し、その後Integration Testを行う。

Checkpointは詳細な作業履歴ではなく、次のAgentが継続するために必要な現在状態だけを残す。

⸻

20. verification.json

機械的検証結果を固定形式で保存する。

{
  "phase_id": "phase-01",
  "run_id": 3,
  "overall": "PASS",
  "checks": [
    {
      "name": "build",
      "status": "PASS",
      "command": "npm run build"
    },
    {
      "name": "test",
      "status": "PASS",
      "command": "npm test"
    },
    {
      "name": "lint",
      "status": "PASS",
      "command": "npm run lint"
    }
  ]
}

詳細ログは別ファイルに保存してよい。

Pipelineが必要とするのは主に PASS / FAIL と実行した検証内容とする。

⸻

21. adversarial-review.json

敵対的レビューの結果。

{
  "phase_id": "phase-01",
  "result": "PASS",
  "findings": []
}

問題がある場合：

{
  "phase_id": "phase-01",
  "result": "FINDINGS",
  "findings": [
    {
      "severity": "high",
      "description": "入力エラー時にフォーム内容が失われる",
      "recommendation": "入力値を保持した状態でValidation Errorを表示する"
    }
  ]
}

Findingが存在する限りHuman Checkへ進まない。

⸻

22. feedback.json

Human Checkで返されたFeedbackを保存する。

{
  "phase_id": "phase-01",
  "result": "FEEDBACK",
  "feedback": "検索後に一覧へ戻ると条件が消える。検索条件を保持してほしい。",
  "created_at": "2026-09-04T03:00:00+09:00"
}

ACCEPTの場合：

{
  "phase_id": "phase-01",
  "result": "ACCEPT",
  "feedback": null,
  "created_at": "2026-09-04T03:00:00+09:00"
}

⸻

23. 固定しないもの

以下は最初のバージョンではSchema化しない。

* Agent内部のPlan
* TODO
* 実装手順
* ファイル変更計画
* Work Item
* Agentの思考過程
* Runごとの詳細な作業予定
* Retry回数や修正戦略

これらを固定すると、Agentの自由度を下げ、Pipelineそのものが重くなるためである。

必要性が確認されたものだけ後から追加する。

⸻

24. 最初の実装範囲

Version 1では以下のみを実現する。

1. Human Requestを受け取る
2. InitiativeとPhaseを生成する
3. Human ApprovalなしでPhase 1を開始する
4. Agent Runを開始する
5. Run終了時にCheckpointを保存する
6. 必要なら次Runで同じPhaseを継続する
7. Phase Goal達成後にVerificationを実行する
8. Verification失敗時はAgentへ差し戻す
9. Verification成功後にAdversarial Reviewを実行する
10. Review FindingがあればAgentへ差し戻す
11. PASSしたPhaseだけHuman Checkへ送る
12. ACCEPTなら次Phaseへ進む
13. FEEDBACKなら同じPhaseのFix Runを開始する
14. Codex / OpenCodeの双方をAgent Interface経由で実行できる

⸻

25. Version 1で作らないもの

以下は必要性が発生するまで実装しない。

* Work Item管理
* 複雑なDependency Graph
* Multi-Agent並列実装
* Agent Scheduler
* 高度なRetry Policy
* 自動優先順位付け
* Human Approval付きPhase Planning
* 詳細なダッシュボード
* 自律Release
* Product Planningの自動化

⸻

26. 最終構造

Human
  │
  │ Request
  ▼
┌──────────────────────────────┐
│ Initiative / Phase Planning  │
└──────────────┬───────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│                 Phase                   │
│                                         │
│   Agent Run                             │
│       ↓                                 │
│   Checkpoint                            │
│       ↓                                 │
│   Agent Run                             │
│       ↓                                 │
│   ...                                   │
│       ↓                                 │
│   Phase Verification                    │
│       ↓                                 │
│   Adversarial Review                    │
│       ↓                                 │
│   Phase Ready                           │
└────────────────────┬────────────────────┘
                     │
                     ▼
               Human Check
                /        \
          FEEDBACK       ACCEPT
             │              │
             ▼              ▼
          Fix Run        Next Phase
             │
             └──────→ Verification

Pipelineの責任は、Agentの作業を細かく管理することではない。

Phaseという人間レビュー単位を維持しながら、必要なだけAgent Runを継続させ、機械検証と敵対的レビューを通過した成果物だけを人間へ提示することを主責務とする。