#!/usr/bin/env python3
"""今日の予約状況を Discord 用に整形して stdout に出す CLI（Phase 7e）

state/YYYY-MM-DD.json と posts/YYYY-MM-DD_5案.md を読み、
予約中 slot の時刻・案メタ・本文を整形する。Bot が出力をそのまま
Discord に貼る運用。

Usage:
  show_schedule.py [--date YYYY-MM-DD | today | tomorrow]
  show_schedule.py --range today,tomorrow
  show_schedule.py --range 2026-05-06,2026-05-07,2026-05-08

設計:
- 既投稿(published): permalink を表示
- 予約中(pending + candidate_id): 時刻・案メタ・本文（【N/M】部分のみ）
- スキップ(skipped): 一行で表示
- 未決(pending + candidate_id=null): 一行で表示
- 自己採点・厚みチェック等の内部メタは出さない（Bot が貼ったとき汚いため）
- セクション間に `\n\n===SPLIT===\n\n` を挿入。Bot は Discord 2000 字制限対策として
  この区切りで分割して複数メッセージで投稿する
- --range 指定時：各日付を順に並べ、日付間に "---" 1 行のセパレータセクションを挟む。
  自動 future セクション（明日以降）は --range モードでは出さない（指定日が明示済のため）
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ACTIVE_DIR = SCRIPT_DIR.parent
JST = timezone(timedelta(hours=9))

SLOT_LABEL = {"morning": "朝", "noon": "昼", "evening": "夜"}
SLOT_TIME = {"morning": "07:00", "noon": "12:00", "evening": "20:00"}

HEADER_RE = re.compile(
    r"^## 案(?P<id>\d+)：(?P<emoji>\S+)\s+(?P<typename>[^／]+?)\s*／\s*形式：(?P<format>[^／]+?)\s*／\s*断定強度：(?P<intensity>\S+)\s*$",
    re.MULTILINE,
)

RESERVATION_FILE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<slot>morning|noon|evening)\.md$"
)
THEME_RE = re.compile(r"【テーマ】\s*(?P<theme>.+?)\s*$", re.MULTILINE)


def die(msg: str, code: int = 1) -> None:
    print(f"[show_schedule.py] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def extract_post_body(post_text: str, candidate_id: int) -> str:
    """指定案の【1/M】〜【M/M】本文だけを抜き出す。

    自己採点・厚みチェック等の内部メタは含めない。
    """
    headers = list(HEADER_RE.finditer(post_text))
    if not headers:
        return ""

    target_idx = None
    for i, h in enumerate(headers):
        if int(h["id"]) == candidate_id:
            target_idx = i
            break
    if target_idx is None:
        return ""

    sec_start = headers[target_idx].start()
    sec_end = headers[target_idx + 1].start() if target_idx + 1 < len(headers) else len(post_text)
    section = post_text[sec_start:sec_end]

    # 【N/M】ブロックだけを連結。【自己採点】以降の内部メタは捨てる
    blocks = re.findall(
        r"【\d+/\d+】\n(.+?)(?=\n【\d+/\d+】|\n【自己採点】|\Z)",
        section,
        flags=re.DOTALL,
    )
    headers_only = re.findall(r"【\d+/\d+】", section)
    headers_only = [h for h in headers_only if not h.startswith("【自己")]

    parts: list[str] = []
    for hdr, body in zip(headers_only, blocks):
        parts.append(f"{hdr}\n{body.rstrip()}")
    if parts:
        return "\n\n".join(parts).rstrip()

    # 1本投稿パターン：【テーマ】XXX\n\n<本文>...（seed candidate / 単発投稿）
    # publish.py の extract_thread_parts と同じフォールバック
    m = re.search(
        r"【テーマ】[^\n]*\n\n(.+?)(?=\n【自己採点】|\n【厚みチェック】|\Z)",
        section,
        flags=re.DOTALL,
    )
    if m:
        return m.group(1).strip()
    return ""


def format_time(scheduled_at: str) -> str:
    """ISO datetime → 'HH:MM' 表示"""
    dt = datetime.fromisoformat(scheduled_at)
    return dt.strftime("%H:%M")


def get_post_text_for_candidate(default_text: str, cand: dict) -> str:
    """candidate.source が set されていれば seeds/ 等のファイルを読む。
    post-drafter エージェント由来の seed candidate は state.post_file に乗らない。"""
    source = cand.get("source")
    if source:
        path = ACTIVE_DIR / source
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return ""
    return default_text


def _prev_day(date: str) -> str:
    """'2026-05-07' → '2026-05-06'。文字列比較が正しく効くように 1 日前を返す。"""
    dt = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def extract_reservation_body(text: str) -> str:
    """予約ファイルから【1/M】〜【M/M】本文を抽出。なければ【テーマ】後の本文を返す。"""
    blocks = re.findall(
        r"【\d+/\d+】\n(.+?)(?=\n【\d+/\d+】|\Z)",
        text,
        flags=re.DOTALL,
    )
    headers_only = re.findall(r"【\d+/\d+】", text)
    if blocks:
        parts: list[str] = []
        for hdr, body in zip(headers_only, blocks):
            parts.append(f"{hdr}\n{body.rstrip()}")
        return "\n\n".join(parts).rstrip()

    # 1 本投稿パターン：【テーマ】XXX\n\n<本文>...
    m = re.search(r"【テーマ】[^\n]*\n\n(.+?)\Z", text, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def scan_future_reservations(after_date: str) -> list[dict]:
    """reservations/<date>_<slot>.md を集約して date > after_date のものだけ返す。

    各エントリ: {date, slot, theme, body, applied, path}
    - applied=True: 対象日 state.json に candidate がある（init 済 or 即時 apply 済）
    - applied=False: state がまだ無い、または apply されていない（deferred）
    """
    reservations_dir = ACTIVE_DIR / "reservations"
    if not reservations_dir.is_dir():
        return []

    out: list[dict] = []
    for path in sorted(reservations_dir.iterdir()):
        if not path.is_file():
            continue
        m = RESERVATION_FILE_RE.match(path.name)
        if not m:
            continue
        date = m.group("date")
        slot = m.group("slot")
        if date <= after_date:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        theme_m = THEME_RE.search(text)
        theme = theme_m.group("theme").strip() if theme_m else "(テーマ未設定)"
        body = extract_reservation_body(text)

        applied = False
        state_path = ACTIVE_DIR / "state" / f"{date}.json"
        if state_path.is_file():
            try:
                st = json.loads(state_path.read_text(encoding="utf-8"))
                source_rel = str(path.relative_to(ACTIVE_DIR))
                applied = any(
                    c.get("source") == source_rel for c in st.get("candidates", [])
                )
            except (OSError, json.JSONDecodeError):
                applied = False

        out.append({
            "date": date,
            "slot": slot,
            "theme": theme,
            "body": body,
            "applied": applied,
            "path": str(path.relative_to(ACTIVE_DIR)),
        })

    # 日付昇順、同日は朝→昼→夜
    slot_order = {"morning": 0, "noon": 1, "evening": 2}
    out.sort(key=lambda r: (r["date"], slot_order.get(r["slot"], 99)))
    return out


def render_future_reservations_summary(future: list[dict]) -> str:
    """明日以降の予約を 1 行ずつ並べたサマリセクション。"""
    lines = ["📅 明日以降の予約", ""]
    for r in future:
        label = SLOT_LABEL.get(r["slot"], r["slot"])
        time_str = SLOT_TIME.get(r["slot"], "")
        marker = "🗓️ 反映済" if r["applied"] else "📥 翌朝反映待ち"
        lines.append(f"・{r['date']} {label} {time_str}：{r['theme']}（{marker}）")
    lines.append("")
    lines.append("（解除：`reservations/YYYY-MM-DD_<slot>.md` を削除）")
    return "\n".join(lines).rstrip()


def render_future_reservation_detail(reservation: dict) -> str:
    """個別予約を 1 セクションとして描画（ヘッダ + 本文）。"""
    label = SLOT_LABEL.get(reservation["slot"], reservation["slot"])
    time_str = SLOT_TIME.get(reservation["slot"], "")
    marker = "🗓️ 反映済" if reservation["applied"] else "📥 翌朝反映待ち"
    head = (
        f"⏰ {reservation['date']} {label} {time_str}（±15分） — "
        f"🗓️ 事前予約（{marker}）\n"
        f"   テーマ：{reservation['theme']}"
    )
    body = reservation.get("body", "")
    if body:
        return head + "\n\n" + body
    return head + "\n\n（本文を抽出できず）"


def render_schedule(state: dict, post_text: str, date: str) -> list[str]:
    """state を読んで Discord 用のセクション群（list[str]）を返す。

    呼び出し側で `\n\n===SPLIT===\n\n` で join する前提。
    """
    cand_by_id = {c["id"]: c for c in state["candidates"]}

    sections: list[str] = []
    has_any = False

    # ヘッダ文言は date が今日/明日/それ以外で出し分け（「今日の予約（2026-05-07）」等の表記揺れを防ぐ）
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    tomorrow_str = (datetime.now(JST) + timedelta(days=1)).strftime("%Y-%m-%d")
    if date == today_str:
        header_label = "今日の予約"
    elif date == tomorrow_str:
        header_label = "明日の予約"
    else:
        header_label = "予約"

    # セクション 1: ヘッダ + 短いサマリ（朝/昼/夜の状態一覧）
    summary_lines = [f"📅 {header_label}（{date}）", ""]
    for slot in state["slots"]:
        slot_name = slot["slot"]
        label = SLOT_LABEL.get(slot_name, slot_name)
        time_str = format_time(slot["scheduled_at"])
        status = slot["status"]
        cand_id = slot.get("candidate_id")
        if status == "published":
            summary_lines.append(f"・{label} {time_str}：✅ 投稿済（案{cand_id}）")
        elif status == "publishing":
            summary_lines.append(f"・{label} {time_str}：🔄 投稿中（案{cand_id}）")
        elif status == "partially_published":
            summary_lines.append(f"・{label} {time_str}：⚠ 一部投稿済（案{cand_id}）")
        elif status == "failed":
            summary_lines.append(f"・{label} {time_str}：❌ 投稿失敗（案{cand_id}）")
        elif status == "skipped":
            err = slot.get("error") or ""
            tag = "明示スキップ" if "explicit" in err else "スキップ"
            summary_lines.append(f"・{label} {time_str}：⏭ {tag}")
        elif status == "reserved":
            summary_lines.append(f"・{label} {time_str}（±15分）：🗓️ 案{cand_id} 事前予約済")
        elif status == "pending" and cand_id is not None:
            summary_lines.append(f"・{label} {time_str}（±15分）：📝 案{cand_id} 予約中")
        elif status == "pending":
            summary_lines.append(f"・{label} {time_str}：⬜ 未決")
        else:
            summary_lines.append(f"・{label} {time_str}：{status}")

    approval = state.get("approval", {})
    summary_lines.append("")
    if approval.get("approved"):
        sel = state.get("selected_ids", [])
        summary_lines.append(f"承認状態：approved（selected_ids={sel}）")
    else:
        summary_lines.append(f"承認状態：{state.get('status', 'unknown')}")
    sections.append("\n".join(summary_lines).rstrip())

    # セクション 2 以降: pending + candidate_id ありの slot を 1 セクションずつ
    for slot in state["slots"]:
        slot_name = slot["slot"]
        label = SLOT_LABEL.get(slot_name, slot_name)
        time_str = format_time(slot["scheduled_at"])
        status = slot["status"]
        cand_id = slot.get("candidate_id")

        if status == "published" and slot.get("permalink"):
            has_any = True
            sec = (
                f"⏰ {label} {time_str} — ✅ 投稿済（案{cand_id}）\n"
                f"   {slot['permalink']}"
            )
            sections.append(sec)
        elif (status == "pending" and cand_id is not None) or status == "reserved":
            has_any = True
            cand = cand_by_id.get(cand_id, {})
            type_ = cand.get("type", "?")
            fmt = cand.get("format", "?")
            intensity = cand.get("intensity", "?")
            theme = cand.get("theme", "(テーマ不明)")
            cand_text = get_post_text_for_candidate(post_text, cand)
            # 予約ファイル由来の candidate は ## 案<数字>： ヘッダを持たない
            # （`## 案<TBD>：🗓️ reservation ...` 形式）ため、HEADER_RE では拾えない。
            # source が reservations/ 配下なら simpler な抽出を使う。
            source = cand.get("source", "") or ""
            if source.startswith("reservations/"):
                body = extract_reservation_body(cand_text)
            else:
                body = extract_post_body(cand_text, cand_id)

            tag = "🗓️ 事前予約" if status == "reserved" else "📝"
            head = (
                f"⏰ {label} {time_str}（±15分） — {tag} 案{cand_id}（{type_}型・{fmt}・断定{intensity}）\n"
                f"   テーマ：{theme}"
            )
            if body:
                sections.append(head + "\n\n" + body)
            else:
                sections.append(head + "\n\n（本文を抽出できず）")

    if not has_any:
        # 予約も投稿もない → サマリだけで終わり
        sections.append("（予約も投稿もありません。`generate.sh` 実行後に承認してください）")

    return sections


def resolve_date_keyword(s: str) -> str:
    """`today` / `tomorrow` / `yesterday` キーワード または `YYYY-MM-DD` を JST 日付文字列に。"""
    s = s.strip()
    today = datetime.now(JST)
    if s == "today":
        return today.strftime("%Y-%m-%d")
    if s == "tomorrow":
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if s == "yesterday":
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    # それ以外は YYYY-MM-DD として検証
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError as exc:
        die(f"日付パース失敗: {s!r}（today / tomorrow / YYYY-MM-DD 形式を指定してください）")
    return s


def render_one_date(date: str) -> list[str]:
    """指定日付の Discord 用セクション群を返す。state があれば render_schedule、無ければ placeholder。"""
    state_path = ACTIVE_DIR / "state" / f"{date}.json"
    post_path = ACTIVE_DIR / "posts" / f"{date}_5案.md"

    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        post_text = ""
        if post_path.is_file():
            post_text = post_path.read_text(encoding="utf-8")
        return render_schedule(state, post_text, date)

    # state がない日付（将来日付の単独照会）。
    # その日付に予約があれば 1 行ずつ並べる。なければプレースホルダだけ。
    same_day = [
        r for r in scan_future_reservations(_prev_day(date))
        if r["date"] == date
    ]
    lines = [f"📅 {date} の予約", ""]
    if same_day:
        for r in same_day:
            label = SLOT_LABEL.get(r["slot"], r["slot"])
            time_str = SLOT_TIME.get(r["slot"], "")
            lines.append(f"・{label} {time_str}：🗓️ {r['theme']}（📥 翌朝反映待ち）")
        lines.append("")
        lines.append("（state はまだありません。generate.sh が走った時点で apply されます）")
    else:
        lines.append("（state ファイルがまだありません。generate.sh が走った時点で作られます）")
    return ["\n".join(lines).rstrip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="予約状況を Discord 用に整形して出力")
    parser.add_argument(
        "--date",
        default=None,
        help="単一日付。`today` / `tomorrow` / `YYYY-MM-DD`。省略時は today",
    )
    parser.add_argument(
        "--range",
        default=None,
        help="複数日付をカンマ区切りで指定（例: today,tomorrow）。--date と排他。"
             " このモードでは末尾の自動 future セクションは出ない（指定日が明示済のため）",
    )
    parser.add_argument(
        "--no-future",
        action="store_true",
        help="末尾の「明日以降の予約」セクションを抑制（--date モード専用）",
    )
    args = parser.parse_args()

    if args.range and args.date:
        die("--range と --date は同時指定できません")

    output_parts: list[str] = []

    if args.range:
        raw_dates = [d for d in args.range.split(",") if d.strip()]
        if not raw_dates:
            die("--range に日付が含まれていません")
        dates = [resolve_date_keyword(d) for d in raw_dates]
        for i, d in enumerate(dates):
            if i > 0:
                output_parts.append("---")
            output_parts.extend(render_one_date(d))
    else:
        date = resolve_date_keyword(args.date) if args.date else datetime.now(JST).strftime("%Y-%m-%d")
        output_parts.extend(render_one_date(date))
        if not args.no_future:
            future = scan_future_reservations(date)
            if future:
                output_parts.append(render_future_reservations_summary(future))
                for r in future:
                    output_parts.append(render_future_reservation_detail(r))

    sys.stdout.write("\n\n===SPLIT===\n\n".join(output_parts) + "\n")


if __name__ == "__main__":
    main()
