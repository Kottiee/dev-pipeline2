# スマートフォン操作ガイド

このガイドは、開発の依頼、進捗確認、承認、修正依頼を行う業務担当者向けです。

## 利用を始める

管理者から案内されたURLを開きます。社外や別のネットワークから利用する場合は、先に端末のTailscaleを接続してください。

iPhoneで通知を使う場合は、Safariの共有メニューから「ホーム画面に追加」を選び、追加されたアイコンから開きます。Androidでは、対応ブラウザーからそのまま利用するか、ホーム画面へ追加します。

画面上部が「接続済み」になれば利用できます。「接続エラー」の場合は[トラブル対応](troubleshooting.md)を確認してください。

## プロジェクトとIssue

画面左上（または上部）の **PROJECT** セレクターから作業対象のプロジェクトを切り替えます。
プロジェクトごとに独立した成果物リポジトリとIssue一覧が表示されます。

- **新規プロジェクト作成**: 「＋ 新規PJ」からプロジェクトIDと（必要に応じて）リポジトリを入力して作成します。
- **新規Issue登録**: プロジェクトを選択した状態で「＋ 新規Issue」を開きます。

## 新しいIssue（依頼）を登録する

1. 対象プロジェクトを選択した状態で「＋ 新規Issue」を開きます。
2. 依頼内容に、実現したいことを入力します。
3. 必要に応じて要件と制約を入力します。
4. 「実行を開始」を選びます。

入力は、実装方法よりも業務上の目的と期待する結果を具体的にします。

入力例：

> 毎月の売上CSVを取り込み、部門別の合計と前月比を確認できる画面を作る。担当者がExcelへ転記できるよう、集計結果をCSVで出力できること。元のCSVは変更しないこと。

依頼を開始すると、AIが計画から最初の成果作成まで進めます。画面を閉じても処理は続きます。

## 進捗を確認する

Issue一覧では、次の状態を確認できます。

| 状態 | 対応 |
|---|---|
| 計画中・実装中・検証中・レビュー中 | AIが作業中です。判断は不要です |
| 承認待ち | 業務担当者による確認が必要です |
| 実行待ち・停止済み | 必要に応じて実行を開始・再開します |
| 実行エラー・中断 | 内容を確認し、解決後に再実行します |
| 完了 (解決済み) | すべてのフェーズが承認され、Issueが完了しました |

Issueを選ぶと、現在のフェーズ、受け入れ条件、テスト結果、レビュー結果、実行ログを確認できます。

修正中も前回のテストやレビュー結果が表示される場合があります。結果に表示されたRun番号と日時を確認し、現在の作業結果と取り違えないようにしてください。

## 承認する

「承認待ち」になったら、次の順で確認します。

1. 「概要」で今回の受け入れ条件を確認します。
2. 「テスト」で自動確認が合格していることを確認します。
3. 「レビュー」で未解決の指摘がないことを確認します。
4. 管理者から案内された方法で成果物を実際に操作します。
5. 業務上問題がなければ「承認して次へ」を選びます。

最後のフェーズではボタンが「承認して完了」になります。

自動テストとAIレビューの合格だけで承認せず、実際の業務手順、表示内容、使い勝手も確認してください。

### スマホで成果物を動作確認する

管理者がWeb/PWAプレビューを設定しているIssueでは、「概要」の「成果物の動作確認」から操作します。

1. 「プレビュー開始」を選びます。
2. 状態が「起動済み」になったら「スマホで動作確認」を選びます。成果物は新しいタブで開きます。
3. 確認後、Issueのタブへ戻って「プレビュー停止」を選びます。

変更を読み直す場合は「再起動」を選びます。起動に失敗した場合は、画面の短いエラーと「プレビュー起動ログ」を管理者へ伝えてください。「未設定」と表示される成果物は従来どおり、管理者から案内された方法で確認します。

プレビューもPipeline画面と同じTailscale接続・利用者制限の内側でだけ開けます。確認URLを外部へ共有しないでください。承認または修正依頼を行うと、起動中のプレビューは自動停止します。

## 修正を依頼する

問題がある場合は「修正を依頼」を選び、コメントを入力します。

伝える内容：

- どの画面や操作で起きたか
- 期待していた結果
- 実際に起きた結果
- 特定のデータだけで起きる場合は、その条件

修正依頼後はAIが同じフェーズを修正し、テストとレビューをやり直します。再び「承認待ち」になってから確認してください。

## 実行を停止・再開する

実行中の案件で「実行を停止」を選ぶと、PC側の処理を止めます。それまでの作業内容は残ります。

再開する場合は、案件を開いて「実行を開始・再開」を選びます。停止直前の処理によっては、同じ作業の一部をやり直す場合があります。

## 通知を設定する

1. 画面右上の「通知設定」を開きます。
2. 「通知を有効にする」を選びます。
3. 端末に確認が出たら通知を許可します。
4. 「テスト通知」で受信できることを確認します。

通知は端末の集中モード、通信状態、OS設定の影響を受けます。通知が届かない場合でも、案件一覧の表示を正式な状態として確認してください。

## 管理者向け設定と運用

ここからは、利用者へURLを案内する管理者向けです。通常の業務担当者が行う作業ではありません。

### 環境分離構成（Run / Dev / Data）

安全な運用のために、実行環境、開発環境、作業データを3つに分離します。いずれも `~/Projects/` 配下に配置します。

| 区分 | 役割 | ディレクトリ | ワークディレクトリ（`--work-dir`） |
|---|---|---|---|
| **Run環境** | launchd が常時動かす本番実行環境 | `~/Projects/dev-pipeline2-runtime`（`main` ブランチ固定） | `~/Projects/dev-pipeline2-data` |
| **Dev環境** | 開発者の編集・ブランチ切替・テスト | `~/Projects/dev-pipeline2` | `~/Projects/dev-pipeline2/work`（直下の `work/`） |
| **Data** | 本番の案件データ・成果物リポジトリ・ログ | `~/Projects/dev-pipeline2-data` | （Run環境が参照） |

開発環境でブランチの切り替えやソース編集を行っても、実行中のRunや次回Runに干渉しません。

### PC内で起動する（Run環境）

Python 3.11以上、Git、利用するAIエージェントを準備し、認証を済ませます。実行用の安定チェックアウトとデータディレクトリを用意します。

```bash
# 実行環境のセットアップ (main ブランチ固定)
git clone https://github.com/Kottiee/dev-pipeline2.git ~/Projects/dev-pipeline2-runtime
cd ~/Projects/dev-pipeline2-runtime
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[web]'

# データディレクトリ作成（新規環境の場合）
mkdir -p ~/Projects/dev-pipeline2-data

# 起動確認
dev-pipeline-web --work-dir /Users/natto/Projects/dev-pipeline2-data
```

PCのブラウザーで `http://127.0.0.1:8787` を開き、画面が表示されることを確認します。

### スマートフォンから接続できるようにする

PCとスマートフォンを同じTailscaleネットワークへ参加させ、PCで次を実行します。

```bash
tailscale serve --bg 8787
```

表示されたHTTPS URLと、利用を許可するTailscaleログイン名を指定してサービスを起動します。

```bash
~/Projects/dev-pipeline2-runtime/.venv/bin/dev-pipeline-web \
  --work-dir ~/Projects/dev-pipeline2-data \
  --public-url https://YOUR-PC.YOUR-TAILNET.ts.net \
  --allow-user user@example.com \
  --push-subject mailto:admin@example.com
```

`--allow-user` は複数指定できます。Tailscale Funnelによる一般公開は行わないでください。Tailscale側でも、接続できる端末と利用者を必要な範囲に限定します。

### 常時利用できる状態にする

サービスをmacOSへのログイン時に起動し、終了時に再起動する場合は、[launchd常時起動ガイド](launchd.md)に従って同梱の `examples/dev-pipeline-web.plist` を設定・登録します。

PCがスリープ、ログアウト、電源切れ、ネットワーク切断になると利用できません。TailscaleやAIサービスの認証切れもplistだけでは復旧しません。

### コードの更新手順（デプロイ）

開発環境（Dev）で機能追加や修正を行った後の本番反映手順です。

1. **開発とテスト**: `~/Projects/dev-pipeline2` で実装し、テストをパスすることを確認します。
2. **マージ**: GitHubの `main` ブランチへマージ（またはプッシュ）します。
3. **実行環境へ反映**:
   ```bash
   cd ~/Projects/dev-pipeline2-runtime
   git pull origin main
   # 依存関係に変更がある場合のみ
   .venv/bin/pip install -e '.[web]'
   ```
4. **サービスの再起動**:
   実行中ジョブがないタイミングで launchd サービスを再起動します。
   ```bash
   launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.dev-pipeline.web.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.dev-pipeline.web.plist
   ```

### 開発環境（Dev）での動作確認

開発環境（`~/Projects/dev-pipeline2`）でWebサーバーやCLIの動作確認を行う際は、本番データ（`dev-pipeline2-data`）に影響を与えないよう、開発環境直下の `work`（`~/Projects/dev-pipeline2/work`）を使用します。
また、launchdで本番サーバーがポート8787で稼働しているため、開発用Webサーバーはポート8788で起動します。

```bash
cd ~/Projects/dev-pipeline2
source .venv/bin/activate

# 開発環境のworkディレクトリと別ポート(8788)でWebサーバーを起動
dev-pipeline-web --work-dir ~/Projects/dev-pipeline2/work --port 8788
```

CLIで動作確認する場合、`dev-pipeline2` ディレクトリ直下では `--work-dir` の既定値が `work`（`./work`）のため、`--work-dir` を省略しても自動的に `work` が参照されます。

```bash
# デフォルトで ./work を参照
dev-pipeline status <案件ID>

# または明示指定
dev-pipeline --work-dir ~/Projects/dev-pipeline2/work status <案件ID>
```

#### 本番案件（dev-pipeline2-data）を開発環境で検証したい場合

本番データ（`dev-pipeline2-data`）上の案件を手元で安全にテスト・再現検証したい場合は、対象案件を開発環境の `work/` へコピーして実行します。本番データや本番実行環境には一切影響しません。

```bash
# 本番から開発環境のworkへ案件データをコピー
cp -r ~/Projects/dev-pipeline2-data/<案件ID> ~/Projects/dev-pipeline2/work/

# 手元で安全に実行・検証
python3 -m dev_pipeline.cli issue run <案件ID> <Issue_ID> --until-ready
```
