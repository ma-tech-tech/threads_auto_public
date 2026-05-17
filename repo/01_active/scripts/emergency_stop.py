#!/usr/bin/env python3
"""緊急停止 CLI（Phase 7f）

本番アカウント (@${THREADS_USERNAME}) への Threads 投稿を 1 コマンドで止める／復旧するための薄い CLI。
publish.py が起動時に state/EMERGENCY_STOP_PRODUCTION の存在をチェックし、
存在 + --profile production の組み合わせなら sys.exit(0) する。

Usage:
  emergency_stop.py --on  [--reason "..."] [--message-id ID] [--actor {discord,cli}]
  emergency_stop.py --off [--message-id ID] [--actor {discord,cli}]
  emergency_stop.py --status

Exit codes:
  0  : --on / --off は常に成功時 0、--status は ON のとき 0
  1  : --status が OFF のとき
  2  : 引数エラー

設計:
- センチネル方式：state/EMERGENCY_STOP_PRODUCTION の存在 / 不在で状態管理
- 冪等：--on を 2 回叩いても 2 重作成しない（最初の reason を保持）／--off を 2 回叩いてもエラーにしない
- stdout は Bot がそのまま Discord に貼れる短文（絵文字 + 1〜3 行）
- 詳細仕様：repo/01_active/緊急停止.md
"""

import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ACTIVE_DIR = SCRIPT_DIR.parent
SENTINEL_PATH = ACTIVE_DIR / "state" / "EMERGENCY_STOP_PRODUCTION"

JST = timezone(timedelta(hours=9))


def _ts() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def _build_sentinel_content(reason: str, actor: str, message_id: str) -> str:
    lines = [
        f"created_at: {_ts()}",
        f"actor: {actor}",
        f"message_id: {message_id or '(なし)'}",
        f"reason: {reason or '(未指定)'}",
    ]
    return "\n".join(lines) + "\n"


def cmd_on(reason: str, actor: str, message_id: str) -> int:
    if SENTINEL_PATH.exists():
        existing = SENTINEL_PATH.read_text(encoding="utf-8").rstrip()
        print("🛑 既に緊急停止 ON です（最初の指定が保持されます）")
        print("```")
        print(existing)
        print("```")
        return 0

    SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENTINEL_PATH.write_text(
        _build_sentinel_content(reason, actor, message_id),
        encoding="utf-8",
    )
    reason_label = reason if reason else "(未指定)"
    print(f"🛑 緊急停止 ON にしました（理由：{reason_label}）")
    print("本番投稿（--profile production）は次回発火からスキップされます。")
    print("解除：/緊急停止解除")
    return 0


def cmd_off(actor: str, message_id: str) -> int:
    if not SENTINEL_PATH.exists():
        print("✅ 緊急停止は元から OFF です（変更なし）")
        return 0

    SENTINEL_PATH.unlink()
    print("✅ 緊急停止を解除しました（次回 launchd 発火から本番投稿が復活します）")
    print("停止中にスキップされた slot がある場合は手動再送：")
    print("`python3 scripts/publish.py --slot SLOT --date YYYY-MM-DD --profile production`")
    return 0


def cmd_status() -> int:
    if SENTINEL_PATH.exists():
        existing = SENTINEL_PATH.read_text(encoding="utf-8").rstrip()
        print("🛑 緊急停止：ON")
        print("```")
        print(existing)
        print("```")
        return 0
    print("✅ 緊急停止：OFF（本番投稿は通常通り動作中）")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="緊急停止 CLI（本番 Threads 投稿のキルスイッチ）"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--on", action="store_true", help="緊急停止 ON（センチネル touch）")
    mode.add_argument("--off", action="store_true", help="緊急停止 OFF（センチネル rm）")
    mode.add_argument("--status", action="store_true", help="現在の状態を表示")

    parser.add_argument("--reason", default="", help="--on 時の停止理由（任意）")
    parser.add_argument(
        "--message-id",
        dest="message_id",
        default="",
        help="Discord メッセージ ID（Bot 経由時）",
    )
    parser.add_argument(
        "--actor",
        default="cli",
        choices=["discord", "cli"],
        help="操作元（cli / discord）",
    )

    args = parser.parse_args()

    if args.on:
        sys.exit(cmd_on(args.reason, args.actor, args.message_id))
    elif args.off:
        sys.exit(cmd_off(args.actor, args.message_id))
    else:
        sys.exit(cmd_status())


if __name__ == "__main__":
    main()
