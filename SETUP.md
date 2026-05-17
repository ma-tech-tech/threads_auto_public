# SETUP — `threads_auto` を別 PC で立ち上げる手順

このドキュメントは GitHub から `git clone` した後、**5 案生成 + 自動投稿 + Discord 承認 Bot** が動く状態にするまでの手順をまとめたもの。

> 前提：macOS / launchd を主環境としているため、launchd 設定は macOS 専用。Linux で動かしたい場合は `repo/01_active/launchd/` を systemd 等に翻訳する必要がある。

---

## 1. リポジトリ取得

```bash
git clone <この repo の URL>
cd threads_auto
export REPO_ROOT="$PWD"
```

> **重要**：launchd plist は `__REPO_ROOT__/...` を **絶対パスでハードコード** している。別ユーザー名・別パスでチェックアウトする場合、§ 5 で plist を書き換えること。

---

## 2. Python 環境

`requests` だけ入っていれば動く（依存はそれだけ）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install requests
```

`.venv/` は gitignore 済み。

---

## 3. `.env` 作成

`.env.example` をコピーして実値を埋める：

```bash
cp .env.example .env
$EDITOR .env
```

埋めるべき値：

| キー | 取得元 |
|---|---|
| `THREADS_ACCESS_TOKEN` | Meta Developers → Threads API → 長期 access token |
| `THREADS_USER_ID` | Threads Graph API `/me` レスポンスの `id` |
| `THREADS_TEST_*` | テスト用アカウント（任意。本番影響を避けたい時だけ） |
| `SUPABASE_*` | Supabase ダッシュボード → Settings → API |
| `THREADS_USERNAME` | Threads handle（@ なし） |
| `DISCORD_WEBHOOK_URL` | Discord サーバー設定 → 連携 → Webhook |
| `DISCORD_APPROVAL_CHANNEL_ID` | 承認チャンネル ID（右クリック → ID コピー。開発者モード必須） |
| `DISCORD_BACKLOG_CHANNEL_ID` | バックログ収集チャンネル ID |
| `DISCORD_DRAFTS_CHANNEL_ID` | ネタ・ドラフト収集チャンネル ID |
| `DISCORD_STATUS_CHANNEL_ID` | `/予約確認` 等の照会専用チャンネル ID |
| `DISCORD_OWNER_USER_ID` | Bot オーナーの Discord user ID（このユーザー以外のメッセージは無視される） |

`.env` は gitignore 済み。**絶対に commit しない**。

---

## 4. Claude Code セットアップ

### 4-1. Claude Code 本体

```bash
# 既に入っていなければ
npm install -g @anthropic-ai/claude-code
```

ログイン状態を確認：

```bash
claude --version
claude auth status   # 未認証なら claude auth login
```

### 4-2. プロジェクト用設定

このリポジトリには `.claude/agents/` と `.claude/settings.json` が含まれている：

```
.claude/
├── agents/
│   ├── backlog-curator.md     # #threads_auto_backlog 担当
│   ├── news-curator.md        # generate.sh Step 1
│   ├── post-drafter.md        # #threads_auto_drafts 担当
│   ├── post-writer.md         # generate.sh Step 2
│   └── ref/                   # 案タイプ定義など参照資料
└── settings.json              # プロジェクト共通設定
```

`.claude/settings.local.json`（マシンローカル設定）と `.claude/scheduled_tasks.lock` 等は gitignore 済み。各マシンで `claude` 起動時に自動生成される。

### 4-3. Discord Bot プラグインを有効化

`claude --channels plugin:discord@claude-plugins-official` を使うため、グローバル設定で該当プラグインを `enabledPlugins=true` にする必要がある（設定済みでなければ）：

```bash
# ~/.claude/settings.json の enabledPlugins["claude-plugins-official"] を確認
cat ~/.claude/settings.json | grep -A2 enabledPlugins
```

無効になっている場合は `claude` 起動 → `/plugins` で対象プラグインを enable する。

### 4-4. Discord Bot Token / 許可リスト

Discord Bot Token と DM/チャンネル許可リストは `discord:configure` / `discord:access` skill で設定する：

```bash
claude
# Claude Code 内で
/discord:configure   # Bot token をペースト
/discord:access      # DM 元 / チャンネルを許可
```

## 5. launchd 設定（macOS）

5 つの自動実行タスクをセットアップする：

| Label | スケジュール | 役割 |
|---|---|---|
| `com.example.threads_auto.generate` | 5:00 | 翌日分 5 案を生成（generate.sh） |
| `com.example.threads_auto.publish_morning` | 7:00 | 朝の自動投稿 |
| `com.example.threads_auto.publish_noon` | 12:00 | 昼の自動投稿 |
| `com.example.threads_auto.publish_evening` | 20:00 | 夜の自動投稿 |
| `com.example.threads_auto.metrics` | 毎時 | Threads Graph API でメトリクス収集 |
| `com.example.threads_auto.channels` | 起動時 | Discord Bot 常駐（claude --channels） |

### 5-1. plist のパス書き換え

`repo/01_active/launchd/*.plist` には `__REPO_ROOT__` / `__HOME__` / `__CLAUDE_BIN__` / `__USER_LOCAL_BIN__` などのプレースホルダが入っている。**全 plist を sed で自環境の絶対パスに書き換える**：

```bash
NEW_REPO_ROOT="$PWD"  # クローン直後ならこれで OK
NEW_CLAUDE_BIN="$(command -v claude)"
NEW_USER_LOCAL_BIN="$HOME/.local/bin"

for f in repo/01_active/launchd/*.plist; do
  sed -i.bak \
    -e "s|__REPO_ROOT__|$NEW_REPO_ROOT|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__CLAUDE_BIN__|$NEW_CLAUDE_BIN|g" \
    -e "s|__USER_LOCAL_BIN__|$NEW_USER_LOCAL_BIN|g" \
    "$f"
done
```

必要に応じて launchd ラベル（`com.example.threads_auto.*`）も自分の reverse-DNS に変えると、他人のリポと混ざらない。

### 5-2. plist インストール

```bash
cp repo/01_active/launchd/*.plist ~/Library/LaunchAgents/
for f in ~/Library/LaunchAgents/com.example.threads_auto.*.plist; do
  launchctl load "$f"
done
launchctl list | grep example.threads_auto   # 6 件並ぶこと
```

### 5-3. `com.example.threads_auto.channels.plist` について

Discord Bot 常駐の plist は **このリポジトリに含まれていない**（マシンローカル）。手動で作るか、別途バックアップから持ってくる。`script -q /dev/null` で pty ラップする必須形式は learning memory `learning_claude_channels_launchd.md` 参照。

---

## 6. 動作確認

```bash
# 1. 5 案生成（手動実行で動作確認）
cd repo/01_active
bash scripts/generate.sh   # ~5 分。posts/<明日>_5案.md と state/<明日>.json が生成される

# 2. Discord に通知が来ることを確認

# 3. Bot 経由で承認テスト（DM か #threads_auto に「1,3,5 で確定」を送る）
#    → state/<明日>.json の selected_ids が更新される

# 4. Threads 投稿テスト（実投稿ではなく test mode）
NOTIFY_IN_TEST=1 python3 scripts/publish.py --slot morning --dry-run
```

---

## 7. ディレクトリ構造（参考）

```
threads_auto/
├── .claude/
│   ├── agents/                 # サブエージェント定義（news-curator / post-writer / post-drafter / backlog-curator）
│   │   └── ref/                # 案タイプ定義など参照資料
│   └── settings.json           # プロジェクト共通設定
├── .env                        # 秘密情報（gitignore）
├── .env.example                # テンプレート
├── .gitignore
├── CLAUDE.md                   # Bot の system prompt
├── README.md
├── SETUP.md                    # このファイル
└── repo/
    └── 01_active/              # 稼働中のスクリプト・ドキュメント
        ├── scripts/            # generate.sh / publish.py / state_update.py 等
        ├── launchd/            # *.plist
        ├── posts/              # 5 案出力（runtime, gitignore）
        ├── seeds/              # post-drafter のドラフト（runtime, gitignore）
        ├── reservations/       # 事前予約（runtime, gitignore）
        ├── state/              # 承認状態（runtime, gitignore）
        ├── learning/edits/     # 補正フィードバック（runtime, gitignore）
        ├── logs/               # 実行ログ（runtime, gitignore）
        ├── 01_運用方針.md
        ├── 05_自動化ロードマップ.md
        ├── 07_構成図.md
        └── 緊急停止.md
```

---

## 8. トラブルシュート

| 症状 | 原因 / 対処 |
|---|---|
| `claude --channels` がすぐ終わる | pty 必須。`script -q /dev/null __CLAUDE_BIN__ --channels ...` でラップ |
| Discord に Bot 反応なし | `enabledPlugins["claude-plugins-official"]=true` を確認。`/plugins` で有効化 |
| `state/<date>.json` が無いと言われる | `bash scripts/generate.sh` を手動実行 |
| `.env` の token 期限切れ | Meta Developers で再発行 → `.env` 更新 → launchd 再読込（`launchctl unload && load`） |
| plist が動かない | `launchctl error <exit-code>` でエラー名確認、StandardErrorPath のログを見る |

---

## 9. 関連ドキュメント

- `CLAUDE.md`：Bot の system prompt（チャンネルルーティング・承認解釈ルール）
- `repo/01_active/01_運用方針.md`：5 案生成 / 3 案選択 / 朝昼晩投稿の運用方針
- `repo/01_active/05_自動化ロードマップ.md`：実装履歴と TODO
- `repo/01_active/07_構成図.md`：システム構成
- `repo/01_active/緊急停止.md`：`/緊急停止` 機能の仕様
