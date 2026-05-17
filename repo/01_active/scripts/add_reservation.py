#!/usr/bin/env python3
"""日付指定スロット予約 CLI（Phase 8b）

数日先の特定 slot に投稿を事前予約する。reservations/<date>_<slot>.md に保存し、
対象日の state.json が既に存在していれば apply_reservations で即時反映する。

Usage:
  add_reservation.py --date YYYY-MM-DD --slot {morning|noon|evening}
                     --body-file <path>
                     [--theme "..."] [--message-id ...] [--actor discord]
                     [--force]

入力:
  --body-file: 【テーマ】... + 【1/M】〜【M/M】 ブロック群（または 1 本投稿の本文）。
               ヘッダ `## 案N：` は **書かない**（このスクリプトが自動付与）。

挙動:
- reservations/<date>_<slot>.md が既に存在すれば --force なしでは die（事故防止）
- ファイル保存: 先頭に `## 案<TBD>：🗓️ reservation ／ 形式：自由 ／ 断定強度：自由` ヘッダ + 空行 + 本文
- 対象日の state.json があれば apply_reservations_for_date() を呼んで即時反映
- 対象日の state.json がまだ無ければ、ファイル保存だけして終わる
  （後日 generate.sh → init_state.py が走った時点で apply_reservations が反映する）
- 同じ message-id で 2 回叩いても冪等
  （既存ファイルの先頭に message-id コメントを書き、見つかれば SKIP）
- stdout に RESERVATION_PATH=... / RESERVATION_DATE=... / RESERVATION_SLOT=... /
  RESERVATION_THEME=... / RESERVATION_APPLIED={true|false} を出力
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ACTIVE_DIR = SCRIPT_DIR.parent
JST = timezone(timedelta(hours=9))

SLOT_NAMES = ("morning", "noon", "evening")

PLACEHOLDER_HEADER = "## 案<TBD>：🗓️ reservation ／ 形式：自由 ／ 断定強度：自由"
THEME_RE = re.compile(r"【テーマ】\s*(?P<theme>.+?)\s*$", re.MULTILINE)
MESSAGE_ID_COMMENT_RE = re.compile(
    r"<!--\s*reservation_message_id:\s*(?P<id>\S+)\s*-->",
)


def _ts() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{_ts()}] [add_reservation.py] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


def validate_date(date_str: str) -> None:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        die(f"--date は YYYY-MM-DD 形式: {date_str!r}")
    today = datetime.now(JST).strftime("%Y-%m-%d")
    if date_str < today:
        die(f"過去日付の予約はできません（today={today}, 指定={date_str}）")


def reservation_path(date: str, slot: str) -> Path:
    return ACTIVE_DIR / "reservations" / f"{date}_{slot}.md"


def extract_theme(body: str) -> str:
    m = THEME_RE.search(body)
    return m.group("theme").strip() if m else ""


def strip_leading_markdown_headers(body: str) -> tuple[str, list[str]]:
    """body 先頭に並ぶ `## ` Markdown ヘッダ行（と直後の連続空行）を全て剥がす。

    post-drafter の Swap モードが seed ファイルから本文を取り出すとき、先頭の
    `## 案N：🌱 seed-...` ヘッダ行を剥がす責務がある（agent doc 7-B-2）。が、
    LLM 側がこれを忘れると、add_reservation.py が build_reservation_text() で
    `## 案<TBD>：🗓️ reservation ...` ヘッダを上に重ねた結果、ファイルに二重
    ヘッダができる。次に publish.py の extract_post_section が
    `^## 案<id>：.+?(?=^## 案\\d+：|\\Z)` で切ろうとすると、最初のヘッダから
    二番目のヘッダ直前までしか拾えず、本文は二番目のヘッダ以降にあるので
    「post body not extracted (neither 【N/M】 nor 【テーマ】 found)」で死ぬ。
    2026-05-15 の朝予約がこれで投稿失敗したため、CLI 入口で防御する。

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
            # 直後の連続する空行も飛ばす
            while i < len(lines) and lines[i].strip() == "":
                i += 1
        else:
            break
    return "\n".join(lines[i:]).strip(), stripped


def find_existing_by_message_id(date: str, message_id: str | None) -> Path | None:
    """同じ Discord message_id で既に登録済みの予約ファイルを探す（冪等性）。"""
    if not message_id:
        return None
    res_dir = ACTIVE_DIR / "reservations"
    if not res_dir.is_dir():
        return None
    for path in res_dir.glob(f"{date}_*.md"):
        try:
            head = path.read_text(encoding="utf-8")[:500]
        except Exception:
            continue
        m = MESSAGE_ID_COMMENT_RE.search(head)
        if m and m.group("id") == message_id:
            return path
    # 別日の予約も検索（同じ Discord メッセージで日付指定したケース）
    for path in res_dir.glob("*.md"):
        try:
            head = path.read_text(encoding="utf-8")[:500]
        except Exception:
            continue
        m = MESSAGE_ID_COMMENT_RE.search(head)
        if m and m.group("id") == message_id:
            return path
    return None


def parse_slot_from_filename(path: Path) -> tuple[str, str]:
    """reservations/YYYY-MM-DD_SLOT.md からファイル名 → (date, slot) を取り出す。"""
    name = path.stem  # YYYY-MM-DD_SLOT
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(morning|noon|evening)$", name)
    if not m:
        die(f"reservation filename が想定外: {path.name}")
    return m.group(1), m.group(2)


def build_reservation_text(body: str, message_id: str | None, theme: str, actor: str) -> str:
    """予約ファイルの全文を構築。先頭に <TBD> ヘッダ、メタコメント、空行、本文。"""
    body = body.strip()
    meta_lines = [
        PLACEHOLDER_HEADER,
        f"<!-- reservation_created_at: {_ts()} -->",
        f"<!-- reservation_actor: {actor} -->",
    ]
    if message_id:
        meta_lines.append(f"<!-- reservation_message_id: {message_id} -->")
    if theme and not THEME_RE.search(body):
        # body に【テーマ】行が無ければ補う
        body = f"【テーマ】{theme}\n\n{body}"
    return "\n".join(meta_lines) + "\n\n" + body + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="日付＋スロット指定で投稿を事前予約")
    parser.add_argument("--date", required=True, help="予約日（YYYY-MM-DD、JST。今日以降）")
    parser.add_argument("--slot", required=True, choices=SLOT_NAMES)
    parser.add_argument("--body-file", required=True,
                        help="【テーマ】行 + 【N/M】 ブロックを含む本文ファイル（## ヘッダなし）")
    parser.add_argument("--theme", help="ヘッダ用テーマ。省略時は body の【テーマ】行から抽出")
    parser.add_argument("--message-id", help="Discord メッセージ ID（冪等性キー）")
    parser.add_argument("--actor", default=os.environ.get("STATE_UPDATE_ACTOR", "cli"))
    parser.add_argument("--force", action="store_true",
                        help="同じ (date, slot) の予約が既にあっても上書き")
    args = parser.parse_args()

    validate_date(args.date)

    body_path = Path(args.body_file).resolve()
    if not body_path.is_file():
        die(f"body file not found: {body_path}")
    body = body_path.read_text(encoding="utf-8").strip()
    if not body:
        die("body file is empty")

    # 防御: post-drafter エージェントが seed の `## 案N：🌱 ...` ヘッダを剥がし
    # 忘れて本文に紛れ込ませると、build_reservation_text() で予約ヘッダを上に
    # 重ねた結果、二重ヘッダのファイルができて publish.py が死ぬ。
    body, stripped_headers = strip_leading_markdown_headers(body)
    if stripped_headers:
        log(
            f"stripped {len(stripped_headers)} leading markdown header(s) from body "
            f"(post-drafter forgot to remove seed header?): {stripped_headers!r}"
        )
    if not body:
        die("body is empty after stripping leading `## ` headers")

    # 冪等性: 同じ Discord message_id で 2 度目に叩かれたら既存を返す
    existing_by_msg = find_existing_by_message_id(args.date, args.message_id)
    if existing_by_msg:
        try:
            ex_date, ex_slot = parse_slot_from_filename(existing_by_msg)
        except SystemExit:
            ex_date, ex_slot = args.date, args.slot
        head = existing_by_msg.read_text(encoding="utf-8")
        body_after_header = re.sub(
            r"^## 案(\d+|<TBD>)：🗓️ reservation .*$\n",
            "",
            head,
            count=1,
            flags=re.MULTILINE,
        ).lstrip()
        theme = extract_theme(body_after_header) or args.theme or "(no theme)"
        log(f"already reserved with message_id={args.message_id}, returning existing file")
        print(f"RESERVATION_PATH={existing_by_msg.relative_to(ACTIVE_DIR)}")
        print(f"RESERVATION_DATE={ex_date}")
        print(f"RESERVATION_SLOT={ex_slot}")
        print(f"RESERVATION_THEME={theme}")
        print("RESERVATION_APPLIED=existing")
        return

    target = reservation_path(args.date, args.slot)
    if target.exists() and not args.force:
        die(
            f"reservation already exists: {target.relative_to(ACTIVE_DIR)}"
            f"\n--force で上書き、または別 slot に予約してください"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    theme = (args.theme or extract_theme(body) or f"reservation_{args.slot}").strip()
    text = build_reservation_text(body, args.message_id, theme, args.actor)
    target.write_text(text, encoding="utf-8")
    log(f"wrote: {target.relative_to(ACTIVE_DIR)} (theme={theme!r})")

    # 対象日の state.json があれば即時反映
    rel_path = str(target.relative_to(ACTIVE_DIR))
    applied_marker = "deferred"
    state_path = ACTIVE_DIR / "state" / f"{args.date}.json"
    if state_path.is_file():
        # 遅延 import: state ロックを伴うので、必要な時だけ読み込む
        from apply_reservations import apply_reservations_for_date

        try:
            results = apply_reservations_for_date(args.date, actor=args.actor)
        except SystemExit as e:
            # apply 側が die した場合はファイルは残るので、用心しつつ終了コード継承
            log(f"apply_reservations failed (rc={e.code}); ファイルは保存済み")
            sys.exit(e.code if isinstance(e.code, int) else 1)
        applied_for_this = next(
            (r for r in results if r["slot"] == args.slot and r["source"] == rel_path),
            None,
        )
        if applied_for_this and applied_for_this["applied"]:
            applied_marker = "applied"
        elif applied_for_this:
            applied_marker = "already_applied"

    print(f"RESERVATION_PATH={rel_path}")
    print(f"RESERVATION_DATE={args.date}")
    print(f"RESERVATION_SLOT={args.slot}")
    print(f"RESERVATION_THEME={theme}")
    print(f"RESERVATION_APPLIED={applied_marker}")


if __name__ == "__main__":
    main()
