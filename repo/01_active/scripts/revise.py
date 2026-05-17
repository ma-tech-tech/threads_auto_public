#!/usr/bin/env python3
"""posts/ 修正 CLI（Phase 7c）

指定された案 N を Claude に再生成させ、posts/ と state.json を更新する。
Discord Bot からも CLI からも同じインターフェイスで叩ける共通 API。

Usage:
  revise.py --date YYYY-MM-DD --candidate N --instruction "..."
            [--message-id ...] [--actor discord]

挙動:
- Claude --print に「N 番の元本文 + 修正指示 + 出力フォーマット注意」を投げる
- 戻ってきた新本文を posts/ の N 番ブロックに差し替え
- state.json を更新:
    - candidates[N-1].revisions[] に履歴追加（discord_message_id で冪等性チェック）
    - candidates[N-1].content_hash 更新
    - candidates[N-1].status = "candidate"
    - selected_ids に N があれば除外、該当 slot を pending にリセット
    - もし approval が立っていたら全リセット（waiting_approval に戻す）
- stdout に新 N 番のセクション全文を出力（Bot が整形して Discord に post）
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ACTIVE_DIR = SCRIPT_DIR.parent
LOG_DIR = ACTIVE_DIR / "logs" / "revisions"
JST = timezone(timedelta(hours=9))

# init_state.py と同じパターン
HEADER_RE = re.compile(
    r"^## 案(?P<id>\d+)：(?P<emoji>\S+)\s+(?P<typename>[^／]+?)\s*／\s*形式：(?P<format>[^／]+?)\s*／\s*断定強度：(?P<intensity>\S+)\s*$",
    re.MULTILINE,
)


def _ts() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def log(msg: str) -> None:
    # stdout は新本文専用なので、ログは stderr に
    print(f"[{_ts()}] [revise.py] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


def load_state(state_path: Path) -> dict:
    if not state_path.is_file():
        die(f"state file not found: {state_path}")
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state: dict, state_path: Path) -> None:
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def append_revision_log(
    *,
    date_str: str,
    candidate_id: int,
    instruction: str,
    before_section: str,
    after_section: str,
    before_hash: str,
    after_hash: str,
    was_selected: bool,
    message_id: str | None,
    actor: str,
) -> None:
    """logs/revisions/YYYY-MM.jsonl に 1 行追記する。

    JST 月次でファイルを切る（投稿日ではなく実行時刻ベース）。
    Phase 2 の analyze_revisions.py が pandas / jq で扱える前提で JSON Lines 形式。
    `category` は将来の自動分類で埋めるため null で予約しておく。

    書き込み失敗は revise 全体を失敗させない（warn のみ）。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(JST)
        log_file = LOG_DIR / f"{now.strftime('%Y-%m')}.jsonl"
        entry = {
            "ts": now.isoformat(timespec="seconds"),
            "date": date_str,
            "candidate_id": candidate_id,
            "actor": actor,
            "message_id": message_id,
            "raw_instruction": instruction,
            "before_section": before_section,
            "after_section": after_section,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "was_selected": was_selected,
            "category": None,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        log(f"revision log appended: {log_file.name}")
    except Exception as e:
        log(f"WARN: failed to write revision log: {e}")


def extract_section(post_text: str, candidate_id: int) -> tuple[str, int, int]:
    """指定 N 番のセクションを抽出。戻り値: (section_text, start_offset, end_offset)"""
    headers = list(HEADER_RE.finditer(post_text))
    if not headers:
        die("案ヘッダ（## 案N：...）が見つからない")

    target = None
    for i, h in enumerate(headers):
        if int(h["id"]) == candidate_id:
            start = h.start()
            end = headers[i + 1].start() if i + 1 < len(headers) else len(post_text)
            target = (post_text[start:end], start, end)
            break

    if target is None:
        die(f"candidate id={candidate_id} のセクションが posts/ に見つからない")
    return target


def extract_body_for_hash(section: str) -> str:
    """init_state.py / publish.py と同じロジック（content_hash 算出用）"""
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


def already_revised(candidate: dict, message_id: str | None) -> bool:
    """同じ Discord message_id の修正が既に記録済みなら True（冪等性）"""
    if not message_id:
        return False
    for rev in candidate.get("revisions", []):
        if rev.get("discord_message_id") == message_id:
            return True
    return False


def build_prompt(section: str, instruction: str) -> str:
    return f"""以下の投稿案を、修正指示に従って書き直してください。

===修正指示===
{instruction}

===元の案===
{section}

===制約===
- タイプ（A/B/C/D/E）と形式（単発／2本スレッド／3本スレッド／4本スレッド）は維持（修正指示で明示的に変更が求められた場合のみ変える）
- 出力フォーマットは元の案と完全に同じ構造を保つ：
  - 1 行目: `## 案N：絵文字 タイプ名 ／ 形式：... ／ 断定強度：...`
  - 【テーマ】行
  - 【1/M】〜【M/M】の本文ブロック
  - 【自己採点】【厚みチェック】【AIっぽさ消し】【主語チェック】（B/C 型は加えて【B型チェック】【C型チェック】も）【根拠】
- 出力は新しい案の Markdown 本体のみ。前置き・後書き・「以下が修正版です」等の説明文・コードブロック（```）禁止
- 1 文字目は必ず `## 案{section.split('案')[1].split('：')[0] if '案' in section else 'N'}：` で始める
"""


def call_claude(prompt: str) -> str:
    """claude --print を呼んで stdout を取得"""
    cmd = [
        "claude",
        "--print",
        "--output-format", "text",
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Read WebSearch WebFetch",
        "--no-session-persistence",
        "--max-budget-usd", "2",
    ]
    log(f"calling: claude --print (prompt {len(prompt)} chars)")
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        die("claude --print timeout (300s)")
    except FileNotFoundError:
        die("claude command not found in PATH")

    if result.returncode != 0:
        die(f"claude --print failed (rc={result.returncode}): {result.stderr[:500]}")

    out = result.stdout.strip()
    if not out:
        die("claude --print returned empty stdout")
    return out


def sanitize_output(raw: str, candidate_id: int) -> str:
    """Claude の出力からコードフェンスを剥がし、## 案N：で始まることを保証"""
    text = raw.strip()
    # ```markdown ... ``` 形式の剥がし
    fence_match = re.match(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # 先頭が ## 案N: で始まらないなら、最初の ## 案N: までスキップ
    expected_header = f"## 案{candidate_id}："
    if not text.startswith(expected_header):
        idx = text.find(expected_header)
        if idx == -1:
            die(f"claude output does not contain '## 案{candidate_id}：' header. raw[:300]={text[:300]!r}")
        text = text[idx:].strip()

    # 末尾改行を 1 個に正規化
    if not text.endswith("\n"):
        text += "\n"
    return text


def replace_section(post_text: str, start: int, end: int, new_section: str) -> str:
    """posts/ の該当セクションを差し替え。前後の改行を整える"""
    before = post_text[:start]
    after = post_text[end:]

    # new_section は末尾改行を 1 個保持
    if not new_section.endswith("\n"):
        new_section += "\n"

    # 元セクションが "\n## 案N+1" の前に "\n" で終わっていた構造を保つため、
    # new_section の末尾に空行を 1 つ足して "\n\n## 案..." の形に揃える
    if after.startswith("##") and not new_section.endswith("\n\n"):
        new_section += "\n"

    return before + new_section + after


def cmd_revise(
    state_path: Path,
    post_path: Path,
    candidate_id: int,
    instruction: str,
    message_id: str | None,
    actor: str,
    date_str: str,
) -> None:
    state = load_state(state_path)

    # candidate 存在確認
    cand_by_id = {c["id"]: c for c in state["candidates"]}
    if candidate_id not in cand_by_id:
        die(f"candidate id={candidate_id} が state に存在しない")
    cand = cand_by_id[candidate_id]

    # 冪等性
    if already_revised(cand, message_id):
        log(f"already revised with message_id={message_id}, skip")
        # stdout には現在の本文を出して Bot が再投稿できるように
        post_text = post_path.read_text(encoding="utf-8")
        section, _, _ = extract_section(post_text, candidate_id)
        sys.stdout.write(section)
        return

    # ガード: 該当 candidate が published slot で使われていれば revise 拒否
    # （投稿後の修正は Threads 側を巻き戻せないので state が壊れる）
    for slot in state.get("slots", []):
        if slot.get("candidate_id") == candidate_id and slot.get("status") == "published":
            die(
                f"candidate {candidate_id} は既に {slot['slot']} slot で投稿済 "
                f"(permalink={slot.get('permalink')})。投稿後の修正は state 不整合に "
                f"なるため revise を拒否します"
            )

    # posts/ 読込
    if not post_path.is_file():
        die(f"post file not found: {post_path}")
    post_text = post_path.read_text(encoding="utf-8")
    old_section, sec_start, sec_end = extract_section(post_text, candidate_id)
    old_hash = cand["content_hash"]

    # claude --print
    prompt = build_prompt(old_section, instruction)
    raw = call_claude(prompt)
    new_section = sanitize_output(raw, candidate_id)

    # 新 hash
    new_body = extract_body_for_hash(new_section)
    new_hash = "sha256:" + hashlib.sha256(new_body.encode("utf-8")).hexdigest()

    if new_hash == old_hash:
        log(f"WARN: new content_hash == old. claude returned identical text? proceeding anyway")

    # posts/ 書き戻し
    new_post_text = replace_section(post_text, sec_start, sec_end, new_section)
    post_path.write_text(new_post_text, encoding="utf-8")
    log(f"posts/ updated (case {candidate_id})")

    # state 更新
    cand["content_hash"] = new_hash
    cand["status"] = "candidate"
    cand.setdefault("revisions", []).append({
        "ts": _ts(),
        "instruction": instruction,
        "discord_message_id": message_id,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "by": actor,
    })

    # selected_ids から除外＆該当 slot リセット
    selected = state.get("selected_ids", [])
    was_selected = candidate_id in selected
    if was_selected:
        state["selected_ids"] = [i for i in selected if i != candidate_id]
        for slot in state.get("slots", []):
            if slot.get("candidate_id") == candidate_id and slot["status"] in ("pending", "publishing", "failed", "partially_published"):
                # 投稿前 / 投稿失敗中のみリセット。published 済みは上のガードで弾いてるのでここには来ない
                slot["candidate_id"] = None
                slot["status"] = "pending"
                slot["error"] = None

    # approval リセット条件: 「投稿予定だった案を修正した」場合のみ
    # = 該当 candidate が selected_ids に入っていた場合のみ
    # 未選択の案を修正しても他 2 案の承認状態は維持される
    approval = state.get("approval", {})
    if was_selected and approval.get("approved"):
        log(f"WARN: candidate {candidate_id} was selected. approval をリセットします（再承認が必要）")
        state["approval"]["approved"] = False
        state["approval"]["approved_at"] = None
        state["approval"]["approved_by"] = None
        state["approval"]["approval_message_id"] = None
        state["approval"]["approval_text"] = None
        state["status"] = "waiting_approval"

    # history
    state.setdefault("history", []).append({
        "ts": _ts(),
        "event": "revised",
        "actor": "revise.py",
        "candidate_id": candidate_id,
        "instruction": instruction,
        "discord_message_id": message_id,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "by": actor,
    })

    save_state(state, state_path)
    log(f"state updated: candidate {candidate_id} hash {old_hash[:14]}... -> {new_hash[:14]}...")

    # 修正ログ追記（Phase 2 の analyze_revisions.py が読む）
    append_revision_log(
        date_str=date_str,
        candidate_id=candidate_id,
        instruction=instruction,
        before_section=old_section,
        after_section=new_section,
        before_hash=old_hash,
        after_hash=new_hash,
        was_selected=was_selected,
        message_id=message_id,
        actor=actor,
    )

    # stdout に新セクション全文（Bot が読んで Discord に post）
    sys.stdout.write(new_section)


def main() -> None:
    parser = argparse.ArgumentParser(description="posts/ の N 案を修正指示で書き直す")
    parser.add_argument("--date", default=datetime.now(JST).strftime("%Y-%m-%d"))
    parser.add_argument("--candidate", type=int, required=True, help="修正対象の案 ID（1〜5）")
    parser.add_argument("--instruction", required=True, help="修正指示（自然言語）")
    parser.add_argument("--message-id", help="Discord メッセージ ID（冪等性キー）")
    parser.add_argument("--actor", default=os.environ.get("STATE_UPDATE_ACTOR", "cli"))
    args = parser.parse_args()

    if not (1 <= args.candidate <= 5):
        die(f"--candidate は 1〜5 の範囲: {args.candidate}")

    state_path = ACTIVE_DIR / "state" / f"{args.date}.json"
    post_path = ACTIVE_DIR / "posts" / f"{args.date}_5案.md"

    cmd_revise(
        state_path=state_path,
        post_path=post_path,
        candidate_id=args.candidate,
        instruction=args.instruction,
        message_id=args.message_id,
        actor=args.actor,
        date_str=args.date,
    )


if __name__ == "__main__":
    main()
