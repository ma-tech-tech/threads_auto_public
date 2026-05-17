#!/usr/bin/env python3
"""seeds/ 登録 CLI（Phase 8 / post-drafter エージェント連携）

post-drafter エージェントがネタから生成した「1 案だけの投稿下書き」を、
state.json の candidates 配列に追加し、posts/ に依存しない seeds/<date>_*.md として
保存する。エージェントから Bash 経由で呼ばれる前提。

Usage:
  add_seed.py --date YYYY-MM-DD --body-file <path> [--theme "..."] [--mode polish|explore]
              [--message-id ...] [--actor discord]

入力:
  --body-file: 【テーマ】... + 【1/M】〜【M/M】 ブロック群（または 1 本投稿の本文）。
               ヘッダ `## 案N：` は **書かない**（このスクリプトが自動付与）。

挙動:
  1. state.json をロックして読み込み
  2. 既存 candidates の最大 id + 1 を新 seed の id とする
  3. seeds/<date>_<HHMMSS>_<slug>.md に `## 案N：🌱 seed ／ ...` ヘッダ付きで保存
  4. state.candidates に { id: N, type: "seed", source: "seeds/...", status: "candidate" } を追加
  5. history に "seed_added" 記録
  6. stdout に SEED_ID=N / SEED_PATH=... / SEED_THEME=... を出力
  7. 同じ message-id で 2 度叩かれたら冪等（既存 seed を再出力するのみ）

設計:
  - 5 案フロー (generate.sh / posts/) には一切干渉しない
  - publish.py / show_schedule.py は candidate.source を見て seed か通常 5 案かを判別
  - state.json の更新は fcntl.flock で排他（publish.py の並行実行と衝突しないため）
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ACTIVE_DIR = SCRIPT_DIR.parent
SEED_ITER_LOG_DIR = ACTIVE_DIR / "logs" / "seed_iterations"
JST = timezone(timedelta(hours=9))

LOCK_TIMEOUT_SEC = 30
LOCK_POLL_INTERVAL_SEC = 0.1


def _ts() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{_ts()}] [add_seed.py] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


@contextmanager
def state_lock(state_path: Path):
    """publish.py と同じ fcntl.flock を使った排他ロック。"""
    lock_path = state_path.with_name(state_path.name + ".lock")
    deadline = time.monotonic() + LOCK_TIMEOUT_SEC
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    try:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    die(f"state lock timeout ({LOCK_TIMEOUT_SEC}s): {lock_path}")
                time.sleep(LOCK_POLL_INTERVAL_SEC)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def extract_theme_from_body(body: str) -> str:
    """本文の 【テーマ】行 を取り出す。なければ空文字。"""
    m = re.search(r"【テーマ】\s*(.+?)\s*$", body, re.MULTILINE)
    return m.group(1).strip() if m else ""


_SLUG_STRIP_RE = re.compile(r"[\s/\\:\*\?\"<>\|【】「」『』〔〕（）()\[\]{},.;:!?。、・…—\-]+")


def slugify(text: str, max_len: int = 24) -> str:
    """ファイル名用 slug。日本語は維持、記号・空白は _ に圧縮。"""
    s = _SLUG_STRIP_RE.sub("_", text.strip()).strip("_")
    s = s[:max_len]
    return s or "untitled"


def find_existing_by_message_id(state: dict, message_id: str | None) -> dict | None:
    """同じ Discord message_id の seed が既に登録済みなら返す（冪等性）。"""
    if not message_id:
        return None
    for cand in state.get("candidates", []):
        if cand.get("seed_message_id") == message_id:
            return cand
    return None


def build_seed_section(candidate_id: int, mode: str, body: str) -> str:
    """seeds/ ファイルの中身。`## 案N：🌱 seed ／ 形式：自由 ／ 断定強度：自由` ヘッダ付き。

    publish.py の extract_post_section / show_schedule.py の HEADER_RE が
    `## 案N：絵文字 タイプ ／ 形式：... ／ 断定強度：...` の形式を期待しているので、
    seed もこの形式で書き出す。タイプ名は `seed-<mode>` とする（polish or explore）。
    """
    body = body.strip()
    type_label = f"seed-{mode}"
    header = f"## 案{candidate_id}：🌱 {type_label} ／ 形式：自由 ／ 断定強度：自由"
    return f"{header}\n\n{body}\n"


def compute_content_hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def strip_leading_markdown_headers(body: str) -> tuple[str, list[str]]:
    """body 先頭に並ぶ `## ` Markdown ヘッダ行（と直後の連続空行）を全て剥がす。

    post-drafter のイテレーション時に、既存 seed の `## 案N：🌱 seed-polish ...`
    ヘッダを剥がし忘れて --body-file に渡すと、build_seed_section() で
    新しい `## 案<next_id>：🌱 seed-...` ヘッダを上に重ねた結果、二重ヘッダの
    seed ファイルができる。これが Swap モードで reservations/ にコピーされると
    publish.py が死ぬ（add_reservation.py 側の同名関数の解説参照）。
    CLI 入口で防御する。

    戻り値: (剥がした後の body, 剥がしたヘッダ行のリスト)。
    """
    stripped: list[str] = []
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            stripped.append(line)
            i += 1
            while i < len(lines) and lines[i].strip() == "":
                i += 1
        else:
            break
    return "\n".join(lines[i:]).strip(), stripped


def _strip_seed_header(text: str) -> str:
    """seeds/ ファイルから先頭の `## 案N：🌱 ...` ヘッダ行を除いた本文を返す。"""
    lines = text.splitlines()
    if lines and lines[0].startswith("## 案"):
        # ヘッダ + その直後の空行 1 つを取り除く
        body_lines = lines[1:]
        if body_lines and body_lines[0].strip() == "":
            body_lines = body_lines[1:]
        return "\n".join(body_lines).rstrip()
    return text.rstrip()


def _read_parent_seed_body(state: dict, parent_seed_id: int) -> str | None:
    """state から parent_seed_id の seed candidate を引いて、source ファイルの本文（ヘッダ除く）を返す。"""
    cand = next((c for c in state.get("candidates", []) if c.get("id") == parent_seed_id), None)
    if not cand:
        return None
    source = cand.get("source")
    if not source:
        return None
    parent_path = ACTIVE_DIR / source
    if not parent_path.is_file():
        return None
    try:
        return _strip_seed_header(parent_path.read_text(encoding="utf-8"))
    except OSError:
        return None


def append_seed_iteration_log(
    *,
    date_str: str,
    seed_id: int,
    parent_seed_id: int | None,
    mode: str,
    theme: str,
    raw_instruction: str | None,
    before_body: str | None,
    after_body: str,
    actor: str,
    message_id: str | None,
) -> None:
    """logs/seed_iterations/YYYY-MM.jsonl に 1 行追記する。

    JST 月次でファイルを切る。post-drafter 経由の seed 生成（初回・イテレーション両方）を残し、
    Phase 2 の analyze_edits.py がチャンネル別分析の入力として使う。
    parent_seed_id が null なら初回生成、int ならその seed_id への iteration。

    書き込み失敗は add_seed.py 全体を失敗させない（warn のみ）。"""
    try:
        SEED_ITER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(JST)
        log_file = SEED_ITER_LOG_DIR / f"{now.strftime('%Y-%m')}.jsonl"
        entry = {
            "ts": now.isoformat(timespec="seconds"),
            "date": date_str,
            "seed_id": seed_id,
            "parent_seed_id": parent_seed_id,
            "mode": mode,
            "theme": theme,
            "raw_instruction": raw_instruction,
            "before_body": before_body,
            "after_body": after_body,
            "actor": actor,
            "message_id": message_id,
            "category": None,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        log(f"seed iteration log appended: {log_file.name}")
    except Exception as e:
        log(f"WARN: failed to write seed iteration log: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="seeds/ に新規 candidate を登録")
    parser.add_argument("--date", default=datetime.now(JST).strftime("%Y-%m-%d"))
    parser.add_argument("--body-file", required=True,
                        help="【テーマ】行 + 【N/M】 ブロックを含む本文ファイル（## ヘッダなし）")
    parser.add_argument("--theme", help="ヘッダ用テーマ。省略時は 【テーマ】行から抽出")
    parser.add_argument("--mode", default="polish", choices=["polish", "explore"],
                        help="生成モード（タイプラベルに使う）")
    parser.add_argument("--message-id", help="Discord メッセージ ID（冪等性キー）")
    parser.add_argument("--actor", default=os.environ.get("STATE_UPDATE_ACTOR", "cli"))
    parser.add_argument(
        "--parent-seed-id",
        type=int,
        default=None,
        help="このイテレーションが派生した元の seed_id。null なら初回生成扱い",
    )
    parser.add_argument(
        "--raw-instruction",
        default=None,
        help="OWNER の自然言語指示（イテレーション時の「もっとタイトに」等）",
    )
    args = parser.parse_args()

    state_path = ACTIVE_DIR / "state" / f"{args.date}.json"
    if not state_path.is_file():
        die(f"state file not found: {state_path}\nヒント: generate.sh が走っていない日は state がありません")

    body_path = Path(args.body_file).resolve()
    if not body_path.is_file():
        die(f"body file not found: {body_path}")
    body = body_path.read_text(encoding="utf-8").strip()
    if not body:
        die("body file is empty")

    # 防御: post-drafter エージェントが既存 seed の `## 案N：🌱 ...` ヘッダを
    # 剥がし忘れて渡してくると、build_seed_section() で新ヘッダを重ねた結果
    # 二重ヘッダの seed ファイルができ、後段の Swap で reservations にコピー
    # された時点で publish.py が死ぬ。CLI 入口で剥がしておく。
    body, stripped_headers = strip_leading_markdown_headers(body)
    if stripped_headers:
        log(
            f"stripped {len(stripped_headers)} leading markdown header(s) from body "
            f"(post-drafter forgot to remove seed header?): {stripped_headers!r}"
        )
    if not body:
        die("body is empty after stripping leading `## ` headers")

    with state_lock(state_path):
        state = json.loads(state_path.read_text(encoding="utf-8"))

        # 冪等性: 同じ Discord message_id で 2 度目に叩かれたら既存 seed を返す
        existing = find_existing_by_message_id(state, args.message_id)
        if existing:
            log(f"already added with message_id={args.message_id}, "
                f"returning existing seed id={existing['id']}")
            print(f"SEED_ID={existing['id']}")
            print(f"SEED_PATH={existing.get('source', '')}")
            print(f"SEED_THEME={existing.get('theme', '')}")
            return

        existing_ids = [c["id"] for c in state.get("candidates", [])]
        next_id = (max(existing_ids) + 1) if existing_ids else 1

        theme = (args.theme or extract_theme_from_body(body) or f"seed{next_id}").strip()

        seeds_dir = ACTIVE_DIR / "seeds"
        seeds_dir.mkdir(parents=True, exist_ok=True)
        ts_compact = datetime.now(JST).strftime("%H%M%S")
        slug = slugify(theme)
        seed_filename = f"{args.date}_{ts_compact}_{slug}.md"
        seed_path = seeds_dir / seed_filename
        seed_section = build_seed_section(next_id, args.mode, body)
        seed_path.write_text(seed_section, encoding="utf-8")

        rel_source = str(seed_path.relative_to(ACTIVE_DIR))
        content_hash = compute_content_hash(body)
        candidate = {
            "id": next_id,
            "type": "seed",
            "format": "自由",
            "intensity": "自由",
            "theme": theme,
            "content_hash": content_hash,
            "status": "candidate",
            "source": rel_source,
            "mode": args.mode,
            "revisions": [],
        }
        if args.message_id:
            candidate["seed_message_id"] = args.message_id
        state.setdefault("candidates", []).append(candidate)

        state.setdefault("history", []).append({
            "ts": _ts(),
            "event": "seed_added",
            "actor": "add_seed.py",
            "candidate_id": next_id,
            "source": rel_source,
            "theme": theme,
            "mode": args.mode,
            "discord_message_id": args.message_id,
            "by": args.actor,
        })

        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"seed added: id={next_id} path={rel_source} theme={theme!r} mode={args.mode}")

    # logs/seed_iterations/YYYY-MM.jsonl に追記（冪等性スキップ時は早期 return 済なのでここには来ない）
    parent_body = _read_parent_seed_body(state, args.parent_seed_id) if args.parent_seed_id else None
    append_seed_iteration_log(
        date_str=args.date,
        seed_id=next_id,
        parent_seed_id=args.parent_seed_id,
        mode=args.mode,
        theme=theme,
        raw_instruction=args.raw_instruction,
        before_body=parent_body,
        after_body=body,
        actor=args.actor,
        message_id=args.message_id,
    )

    print(f"SEED_ID={next_id}")
    print(f"SEED_PATH={rel_source}")
    print(f"SEED_THEME={theme}")


if __name__ == "__main__":
    main()
