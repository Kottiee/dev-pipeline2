# macOS launchd常時起動ガイド

このガイドは、`dev-pipeline-web` をmacOSへのログイン時に起動し、終了時に自動再起動する管理者向け手順です。同梱の `examples/dev-pipeline-web.plist` をLaunchAgentとして使用します。

## 前提

- Python仮想環境へ `python -m pip install -e '.[web]'` を実行済み
- 利用するAgent CLIの認証済み
- `dev-pipeline-web` をフォアグラウンドで起動できる
- スマートフォン公開時はTailscale Serveを設定済み
- plist内で指定する全パスが絶対パス

まずlaunchdを使わずに起動確認します。

```bash
/絶対パス/dev-pipeline2-runtime/.venv/bin/python \
  -m dev_pipeline.web_server \
  --work-dir /絶対パス/dev-pipeline2-data
```

`http://127.0.0.1:8787` を開けることを確認し、`Ctrl+C` で停止します。

## 1. Tailscale Serveを設定する

PC内だけで使う場合、この手順は不要です。スマートフォンから接続する場合は、Webサーバーと同じポートをTailscale Serveへ登録します。

```bash
tailscale serve --bg 8787
tailscale serve status
```

表示されたパスなしのHTTPS URLを、plistの `--public-url` に指定します。Tailscale Funnelによる一般公開は使用しません。

## 2. plistを配置する

LaunchAgentとログのディレクトリを作り、サンプルをコピーします。

```bash
mkdir -p ~/Library/LaunchAgents
mkdir -p ~/Library/Logs/dev-pipeline-web
cp examples/dev-pipeline-web.plist \
  ~/Library/LaunchAgents/local.dev-pipeline.web.plist
```

コピー先のplistで、少なくとも次を実環境に合わせます。

| 項目 | 設定内容 |
|---|---|
| Pythonパス | 仮想環境内の `.venv/bin/python` の絶対パス |
| `--work-dir` | 案件状態を保存するディレクトリの絶対パス |
| `--public-url` | Tailscale Serveが表示したHTTPS URL。ローカル専用なら引数ごと削除 |
| `--allow-user` | 許可するTailscaleログイン名。複数人は引数を繰り返す |
| `--push-subject` | Web Push管理者の連絡先（例: `mailto:admin@example.com`） |
| `WorkingDirectory` | 実行環境プロジェクトの絶対パス（例: `~/Projects/dev-pipeline2-runtime`） |
| `PATH` | 仮想環境とAgent CLIを含む検索パス |
| `StandardOutPath` | 標準出力ログの絶対パス |
| `StandardErrorPath` | 標準エラーログの絶対パス |

複数の許可ユーザーを設定する例：

```xml
<string>--allow-user</string><string>user1@example.com</string>
<string>--allow-user</string><string>user2@example.com</string>
```

`ProgramArguments` の各引数は別々の `<string>` にします。シェルの引用符や行継続記号は記述しません。

Agent CLIの場所は、通常のターミナルで次のコマンドを実行して確認します。

```bash
command -v codex
command -v opencode
command -v agy
```

このMacの同梱plistは、現在確認できる3つのAgentをすべて呼び出せるよう、次のディレクトリを `PATH` に設定しています。

```text
/Applications/ChatGPT.app/Contents/Resources  # codex
/Users/natto/.opencode/bin                    # opencode
/Users/natto/.local/bin                       # agy (Antigravity)
```

Agentを更新・再インストールして `command -v` の結果が変わった場合は、plistの `PATH` も更新して再登録します。`PATH` にはコマンド本体ではなく、その親ディレクトリを追加します。パスに空白があっても、plistの `<string>` 内では引用符を追加しません。

launchdは `.zshrc` などのシェル初期化ファイルを読みません。そのため、ターミナルのPATHだけにAgentの場所を追加してもWebサービスからは見つかりません。使用しないAgentのパスは省略できますが、Web UIでAgentを切り替える場合は、候補となる各CLIの親ディレクトリがすべて必要です。

## 3. plistを検証する

```bash
plutil -lint ~/Library/LaunchAgents/local.dev-pipeline.web.plist
```

`OK` になるまで登録しません。ログ出力先の親ディレクトリも先に存在する必要があります。

## 4. 登録して起動する

```bash
launchctl bootstrap \
  gui/$(id -u) \
  ~/Library/LaunchAgents/local.dev-pipeline.web.plist
```

`RunAtLoad` が有効なため、登録直後に起動します。状態を確認します。

```bash
launchctl print gui/$(id -u)/local.dev-pipeline.web
curl -fsS http://127.0.0.1:8787/api/config
```

ブラウザーでもローカルURLまたはTailscale URLを開きます。

## 5. 設定を変更する

plistを編集しただけでは実行中プロセスへ反映されません。いったん登録解除してから再登録します。

```bash
launchctl bootout \
  gui/$(id -u) \
  ~/Library/LaunchAgents/local.dev-pipeline.web.plist

plutil -lint ~/Library/LaunchAgents/local.dev-pipeline.web.plist

launchctl bootstrap \
  gui/$(id -u) \
  ~/Library/LaunchAgents/local.dev-pipeline.web.plist
```

同じLabelが登録済みの状態で `bootstrap` すると失敗します。その場合も先に `bootout` します。

## 6. ログを確認する

サンプル設定のログは次の2ファイルです。

```bash
tail -n 100 ~/Library/Logs/dev-pipeline-web/pipeline-web.log
tail -n 100 ~/Library/Logs/dev-pipeline-web/pipeline-web-error.log
```

案件ごとのWebジョブログは `<work-dir>/.web/job-logs/`、Agentの詳細ログは `<work-dir>/<案件ID>/agent-logs/` に保存されます。

起動しない場合は、次を順に確認します。

1. `plutil -lint` が成功する
2. plistのPython、`WorkingDirectory`、`--work-dir`、ログパスが絶対パスで存在する
3. `PATH` から利用するAgent CLIを見つけられる
4. 同じ `--work-dir` のWebサーバーが別プロセスで起動していない
5. Agent CLIの認証がログイン後のユーザー環境で有効
6. `StandardErrorPath` と案件のジョブログに原因が出ていないか

plistと同じPATHで3つのAgentを解決できるか確認する例：

```bash
env PATH="/Users/natto/Projects/dev-pipeline2-runtime/.venv/bin:/Applications/ChatGPT.app/Contents/Resources:/Users/natto/.opencode/bin:/Users/natto/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
  /bin/zsh -c 'command -v codex; command -v opencode; command -v agy'
```

## 7. 停止・登録解除する

```bash
launchctl bootout \
  gui/$(id -u) \
  ~/Library/LaunchAgents/local.dev-pipeline.web.plist
```

再び登録するまで自動起動しません。plist自体が不要なら、登録解除後に `~/Library/LaunchAgents/local.dev-pipeline.web.plist` を削除します。

## 常時起動の範囲

- `RunAtLoad` はユーザーがmacOSへログインしたときに起動します。ログアウト中はLaunchAgentが動きません。
- `KeepAlive` はプロセス終了後に再起動します。設定不備があると再起動と失敗を繰り返すため、エラーログを確認してください。
- Macのスリープ、電源断、ネットワーク切断中は接続できません。
- Tailscale Serveの状態とAIサービスの認証切れは、このplistでは復旧しません。
- サービス終了時、実行中だった案件は中断として扱われます。ファイル変更は残り、状態確認後に再開できます。
