#!/usr/bin/env python3
"""state.json 初期化スクリプト（Phase 3 / 7a）

posts/YYYY-MM-DD_5案.md を解析し、state/YYYY-MM-DD.json を書き出す。
generate.sh から呼ばれるが、手動実行も可能。

Usage:
  init_state.py [--date YYYY-MM-DD] [--force]

設計判断:
- 5 案を candidates[] に展開（id / type / format / intensity / theme / content_hash）
- 全 slot は status=pending / candidate_id=null（承認後に candidate_id が決まる）
- 全体 status=waiting_approval（DM/チャンネルで「1,3,5 で確定」を待つ）
- 既存 state ファイルがあれば上書きしない（--force で強制）
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ACTIVE_DIR = SCRIPT_DIR.parent

JST = timezone(timedelta(hours=9))

# 案見出しのパターン: "## 案1：🅰️ データドリブン型 ／ 形式：2本スレッド ／ 断定強度：強"
HEADER_RE = re.compile(
    r"^## 案(?P<id>\d+)：(?P<emoji>\S+)\s+(?P<typename>[^／]+?)\s*／\s*形式：(?P<format>[^／]+?)\s*／\s*断定強度：(?P<intensity>\S+)\s*$",
    re.MULTILINE,
)
THEME_RE = re.compile(r"【テーマ】\s*(?P<theme>.+?)\s*$", re.MULTILINE)

# 🅰️🅱️🅲️🅳️🅴️ → A B C D E
EMOJI_TO_TYPE = {
    "🅰️": "A",
    "🅱️": "B",
    "🅲️": "C",
    "🅳️": "D",
    "🅴️": "E",
}

# ロギング
def _ts() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")

def log(msg: str) -> None:
    print(f"[{_ts()}] [init_state.py] {msg}", flush=True)

def die(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


def parse_candidates(post_text: str) -> list[dict]:
    """posts/ の md から 5 案分のメタ情報＋ content_hash を抽出"""
    headers = list(HEADER_RE.finditer(post_text))
    if not headers:
        die("案ヘッダ（## 案N：...）が見つからない")

    candidates = []
    for i, h in enumerate(headers):
        cand_id = int(h["id"])
        emoji = h["emoji"]
        type_letter = EMOJI_TO_TYPE.get(emoji, emoji)
        fmt = h["format"].strip()
        intensity = h["intensity"].strip()

        # 案セクション全体（次の案ヘッダ or EOF まで）
        start = h.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(post_text)
        section = post_text[start:end]

        # テーマ
        m = THEME_RE.search(section)
        theme = m["theme"].strip() if m else ""

        # content_hash: 投稿対象テキスト（【N/M】 or 【テーマ】後の本文）の sha256
        # 案セクション全体をハッシュすると【自己採点】等も含まれて生成のたびに変わるので、
        # 投稿に使う部分だけのハッシュにする。簡易実装としてセクション全体でも実用上は十分だが、
        # ここでは publish.py と同じ抽出ロジックで本文だけ取り出す。
        body_for_hash = _extract_body_for_hash(section)
        content_hash = "sha256:" + hashlib.sha256(body_for_hash.encode("utf-8")).hexdigest()

        candidates.append({
            "id": cand_id,
            "type": type_letter,
            "format": fmt,
            "intensity": intensity,
            "theme": theme,
            "content_hash": content_hash,
            "status": "candidate",
            "revisions": [],
        })

    return candidates


def _extract_body_for_hash(section: str) -> str:
    """publish.py の extract_thread_parts と同じロジック"""
    parts = []
    matches = list(re.finditer(
        r"【(\d+)/(\d+)】\s*\n(.+?)(?=\n【\d+/\d+】|\n【自己採点】|\n【厚みチェック】|\Z)",
        section,
        re.DOTALL,
    ))
    if matches:
        ordered = sorted(matches, key=lambda m: int(m.group(1)))
        return "\n---\n".join(m.group(3).strip() for m in ordered)

    m = re.search(
        r"【テーマ】[^\n]*\n\n(.+?)(?=\n【自己採点】|\n【厚みチェック】|\Z)",
        section,
        re.DOTALL,
    )
    if m:
        return m.group(1).strip()
    return section


def build_state(date: str, post_file_rel: str, candidates: list[dict]) -> dict:
    now = _ts()
    return {
        "schema_version": 1,
        "date": date,
        "status": "waiting_approval",
        "generation_id": f"{date}-0500-001",
        "generated_at": now,
        "post_file": post_file_rel,

        "discord": {
            "channel_id": None,
            "message_id": None,
            "thread_id": None,
            "approval_message_id": None,
        },

        "candidates": candidates,
        "selected_ids": [],

        "approval": {
            "approved": False,
            "approved_at": None,
            "approved_by": None,
            "approval_message_id": None,
            "approval_text": None,
        },

        "slots": [
            {
                "slot": "morning",
                "scheduled_at": f"{date}T07:00:00+09:00",
                "candidate_id": None,
                "status": "pending",
                "profile": None,
                "parent_id": None,
                "reply_ids": [],
                "permalink": None,
                "published_at": None,
                "retry_count": 0,
                "error": None,
                "metrics_24h": None,
            },
            {
                "slot": "noon",
                "scheduled_at": f"{date}T12:00:00+09:00",
                "candidate_id": None,
                "status": "pending",
                "profile": None,
                "parent_id": None,
                "reply_ids": [],
                "permalink": None,
                "published_at": None,
                "retry_count": 0,
                "error": None,
                "metrics_24h": None,
            },
            {
                "slot": "evening",
                "scheduled_at": f"{date}T20:00:00+09:00",
                "candidate_id": None,
                "status": "pending",
                "profile": None,
                "parent_id": None,
                "reply_ids": [],
                "permalink": None,
                "published_at": None,
                "retry_count": 0,
                "error": None,
                "metrics_24h": None,
            },
        ],

        "history": [
            {"ts": now, "event": "generated", "actor": "generate.sh"},
            {"ts": now, "event": "state_initialized", "actor": "init_state.py", "candidates_count": len(candidates)},
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="state.json 初期化（posts/ → state/）")
    parser.add_argument("--date", default=datetime.now(JST).strftime("%Y-%m-%d"))
    parser.add_argument("--force", action="store_true", help="既存の state ファイルを上書き")
    args = parser.parse_args()

    post_file = ACTIVE_DIR / "posts" / f"{args.date}_5案.md"
    state_file = ACTIVE_DIR / "state" / f"{args.date}.json"

    if not post_file.is_file():
        die(f"post file not found: {post_file}")

    if state_file.is_file() and not args.force:
        log(f"state file already exists: {state_file} (use --force to overwrite)")
        sys.exit(0)

    log(f"reading: {post_file}")
    post_text = post_file.read_text(encoding="utf-8")
    candidates = parse_candidates(post_text)

    log(f"parsed candidates: {len(candidates)}")
    for c in candidates:
        log(f"  [{c['id']}] type={c['type']} format={c['format']} intensity={c['intensity']} theme={c['theme'][:40]}...")

    state = build_state(
        date=args.date,
        post_file_rel=f"posts/{args.date}_5案.md",
        candidates=candidates,
    )

    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"wrote: {state_file}")

    # 予約済みスロット（reservations/<date>_<slot>.md）があれば即反映。
    # apply_reservations はロックを取って state を更新するので、ここで呼ぶことで
    # state ファイル新規作成時から予約が乗った状態になる。
    try:
        from apply_reservations import apply_reservations_for_date

        applied = apply_reservations_for_date(args.date, actor="init_state.py")
        if applied:
            for r in applied:
                marker = "+" if r["applied"] else "="
                log(
                    f"reservation [{marker}] slot={r['slot']} candidate_id={r['candidate_id']} "
                    f"theme={r['theme'][:40]!r}"
                )
    except SystemExit:
        # apply_reservations が die した場合は state は残ったまま終了
        raise
    except Exception as e:
        log(f"WARN: apply_reservations failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
