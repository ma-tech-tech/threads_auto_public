# threads_auto

Claude Code を中心に組んだ Threads 自動投稿システム。news-curator / post-writer / post-drafter / backlog-curator の 4 つのサブエージェントと launchd + Discord Bot で、ネタ収集 → 5 案生成 → Discord での承認 → 自動投稿 → メトリクス収集まで回す。

これは個人運用しているリポジトリのフレームワーク部分を sanitize して公開したものです。識別子（Threads handle, Discord channel ID, ユーザー ID, ローカルパス, launchd ラベル）は全てプレースホルダか環境変数化されており、`.env` と一部 plist を埋めれば自分用に動かせる構成にしてあります。

> Note: このリポの中身は「自分が運用してたら使い物になった」を 1 個の参考実装として公開する目的のもので、fork してそのまま動かせる完成度を狙ったテンプレートではありません。Threads / Discord / Supabase の API トークンや個別のクラウド設定は自分で用意してください。

## 何が入っているか

- **`repo/01_active/scripts/`** — 生成 / 投稿 / 承認 / 修正 / 予約 / 緊急停止の CLI（Python + shell）
- **`repo/01_active/launchd/`** — generate / publish_{morning,noon,evening} / metrics の launchd plist テンプレート
- **`.claude/agents/`** — Claude Code のサブエージェント定義（news-curator, post-writer, post-drafter, backlog-curator）
- **`CLAUDE.md`** — Discord Bot 常駐セッションの system prompt（チャンネルルーティング、メッセージ解釈、承認フロー、修正指示、予約確認、緊急停止）
- **`repo/01_active/01_運用方針.md`** — 編集デスク兼アカウント運用の方針
- **`repo/01_active/05_自動化ロードマップ.md`** — Phase 1〜4 の実装計画と進捗の正本
- **`repo/01_active/07_構成図.md`** — 全体アーキテクチャ図 + データフロー
- **`repo/01_active/緊急停止.md`** — `/緊急停止` 機能の仕様

## プレースホルダ規則

このリポでは以下のプレースホルダを使っています。fork 後に sed 等で自分の値に置換してください（詳細は `SETUP.md` § 5）。

| プレースホルダ | 意味 | 例 |
|---|---|---|
| `__REPO_ROOT__` | このリポをチェックアウトした絶対パス | `/Users/you/projects/threads_auto` |
| `__HOME__` | ホームディレクトリ | `/Users/you` |
| `__CLAUDE_BIN__` | `claude` CLI の絶対パス | `/Users/you/.local/bin/claude` |
| `__USER_LOCAL_BIN__` | `$HOME/.local/bin` 相当 | `/Users/you/.local/bin` |
| `${THREADS_USERNAME}` | Threads handle（@ なし） | `.env` から読む |
| `${DISCORD_APPROVAL_CHANNEL_ID}` ほか 4 種 | Discord チャンネル ID | `.env` から読む |
| `${DISCORD_OWNER_USER_ID}` | Bot オーナーの Discord user ID | `.env` から読む |
| `com.example.threads_auto.*` | launchd ラベル名 | fork 時に自分の reverse-DNS に書き換え推奨 |

## セットアップ

`SETUP.md` を参照。

- `.env.example` → `.env` にコピーして埋める
- `repo/01_active/launchd/*.plist` の `__REPO_ROOT__` / `__HOME__` を自環境に sed 置換
- `launchctl load` で 5 つの plist をロード
- Claude Code の Discord plugin（claude-plugins-official）を有効化して `claude --channels plugin:discord@claude-plugins-official` を常駐

## ライセンス

未定。気が向いたら追加します。

## 関連

- [Threads Graph API](https://developers.facebook.com/docs/threads)
- [Claude Code](https://docs.claude.com/claude-code)
