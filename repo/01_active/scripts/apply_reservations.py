#!/usr/bin/env python3
"""日付指定スロット予約を state.json に取り込むユーティリティ（Phase 8b）

reservations/<date>_<slot>.md を state.json の candidates 配列に追加し、
該当 slot を status=reserved / candidate_id=N に固定する。

Usage（CLI 単独）:
  apply_reservations.py --date YYYY-MM-DD [--actor system]

Usage（ライブラリ）:
  from apply_reservations import apply_reservations_for_date
  applied = apply_reservations_for_date(date_str, actor="init_state.py")

挙動:
- state/<date>.json をロックして更新（add_seed.py と同じ fcntl.flock）
- reservations/<date>_morning.md / _noon.md / _evening.md の存在を順にチェック
- 既に source=reservations/<date>_<slot>.md な candidate があれば SKIP（冪等）
  - そのとき slot 側が違う candidate を指していたら警告ログ（手動修復が必要）
- なければ candidates に新規追加（id=max+1, type=reservation, status=approved）
- ファイル先頭の `## 案<TBD>：🗓️ reservation ...` ヘッダの <TBD> を実 id に置換
- slot.status = "reserved", slot.candidate_id = N, slot.error = None
- 既存 slot の status が publishing/published/failed/partially_published なら die
  （既に投稿パイプに入っているスロットを予約で上書きするのは事故）
- approval は触らない（5 案承認とは独立）
- candidate.status = "approved"（予約は事前承認扱い）
- history に "reservation_applied" 追加
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
JST = timezone(timedelta(hours=9))

LOCK_TIMEOUT_SEC = 30
LOCK_POLL_INTERVAL_SEC = 0.1

SLOT_NAMES = ("morning", "noon", "evening")
LOCKED_SLOT_STATUSES_FOR_RESERVE = (
    "publishing",
    "published",
    "failed",
    "partially_published",
)

PLACEHOLDER_HEADER_RE = re.compile(
    r"^## 案(?P<id>\d+|<TBD>)：🗓️ reservation .*$",
    re.MULTILINE,
)
RESERVATION_HEADER_TEMPLATE = "## 案{id}：🗓️ reservation ／ 形式：自由 ／ 断定強度：自由"

THEME_RE = re.compile(r"【テーマ】\s*(?P<theme>.+?)\s*$", re.MULTILINE)


def _ts() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{_ts()}] [apply_reservations.py] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


@contextmanager
def state_lock(state_path: Path):
    """publish.py / add_seed.py と同じ fcntl.flock を使った排他ロック。"""
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


def reservation_path(date: str, slot: str) -> Path:
    return ACTIVE_DIR / "reservations" / f"{date}_{slot}.md"


def list_existing_reservations(date: str) -> list[tuple[str, Path]]:
    """[(slot_name, path), ...] 順は morning, noon, evening。存在するものだけ返す。"""
    out: list[tuple[str, Path]] = []
    for slot in SLOT_NAMES:
        path = reservation_path(date, slot)
        if path.is_file():
            out.append((slot, path))
    return out


def find_existing_reservation_candidate(state: dict, source_rel: str) -> dict | None:
    """既に同じ source の予約 candidate が candidates にあるか。"""
    for cand in state.get("candidates", []):
        if cand.get("source") == source_rel:
            return cand
    return None


def extract_theme(body: str) -> str:
    m = THEME_RE.search(body)
    return m.group("theme").strip() if m else ""


def compute_content_hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def rewrite_header_id(text: str, candidate_id: int) -> tuple[str, bool]:
    """ファイル先頭の `## 案<TBD>：🗓️ reservation ...` ヘッダの <TBD> を実 id に置換。
    既に digit の場合も同じ id に揃える。戻り値は (新テキスト, ヘッダ書き換え発生したか)。"""
    new_header = RESERVATION_HEADER_TEMPLATE.format(id=candidate_id)
    matches = list(PLACEHOLDER_HEADER_RE.finditer(text))
    if not matches:
        die(
            "予約ファイルにヘッダ `## 案<TBD>：🗓️ reservation ...` が見つかりません。"
            "add_reservation.py が書いたフォーマットになっているか確認してください。"
        )
    m = matches[0]
    new_text = text[: m.start()] + new_header + text[m.end():]
    return new_text, new_text != text


def apply_reservations_for_date(date: str, actor: str = "system") -> list[dict]:
    """指定日の reservations/ を state.json に反映。
    戻り値は [{"slot": ..., "candidate_id": ..., "theme": ..., "source": ..., "applied": bool}].
    """
    reservations = list_existing_reservations(date)
    state_path = ACTIVE_DIR / "state" / f"{date}.json"

    if not reservations:
        return []

    if not state_path.is_file():
        # state がまだない（例：将来日付） → 何もしない。
        # generate.sh → init_state.py が当日 state を作るときに再度呼ばれる。
        log(f"state file not yet exists for {date}; skipping apply (will retry on init)")
        return []

    results: list[dict] = []

    with state_lock(state_path):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        slots_by_name = {s["slot"]: s for s in state["slots"]}

        for slot_name, path in reservations:
            if slot_name not in slots_by_name:
                log(f"WARN: slot {slot_name} が state に無いのでスキップ: {path}")
                continue

            slot = slots_by_name[slot_name]
            source_rel = str(path.relative_to(ACTIVE_DIR))

            existing = find_existing_reservation_candidate(state, source_rel)
            if existing:
                # 冪等パス。slot 側が予約 candidate を指していなければ修復。
                applied = False

                # add_reservation.py --force でファイルが `## 案<TBD>：` ヘッダ + 新本文に
                # 上書きされている可能性があるので、ファイルを読み直して
                # ヘッダ・theme・content_hash を candidate に合わせて再同期する。
                # （これをやらないと publish.py が「案N not found」で落ちる）
                text = path.read_text(encoding="utf-8")
                new_text, header_changed = rewrite_header_id(text, existing["id"])
                if header_changed:
                    path.write_text(new_text, encoding="utf-8")
                    log(f"resynced stale header in {source_rel}: -> 案{existing['id']}")
                    applied = True

                body_after_header = PLACEHOLDER_HEADER_RE.sub("", new_text, count=1).strip()
                new_theme = extract_theme(body_after_header) or existing.get("theme", "")
                new_hash = compute_content_hash(body_after_header)
                if existing.get("content_hash") != new_hash:
                    existing["content_hash"] = new_hash
                    existing["theme"] = new_theme
                    log(
                        f"resynced candidate id={existing['id']} metadata "
                        f"(theme/hash refreshed from {source_rel})"
                    )
                    applied = True

                if slot["status"] in LOCKED_SLOT_STATUSES_FOR_RESERVE and slot.get("candidate_id") != existing["id"]:
                    log(
                        f"WARN: slot {slot_name} status={slot['status']} で予約 candidate id={existing['id']} と "
                        "競合。手動修復が必要かもしれません。slot は触りません。"
                    )
                elif slot.get("candidate_id") != existing["id"] or slot["status"] not in (
                    "reserved",
                    "publishing",
                    "published",
                    "partially_published",
                    "failed",
                ):
                    slot["candidate_id"] = existing["id"]
                    slot["status"] = "reserved"
                    slot["error"] = None
                    applied = True

                if applied:
                    state.setdefault("history", []).append(
                        {
                            "ts": _ts(),
                            "event": "reservation_resynced",
                            "actor": "apply_reservations.py",
                            "slot": slot_name,
                            "candidate_id": existing["id"],
                            "source": source_rel,
                            "theme": existing.get("theme", ""),
                            "by": actor,
                        }
                    )

                results.append(
                    {
                        "slot": slot_name,
                        "candidate_id": existing["id"],
                        "theme": existing.get("theme", ""),
                        "source": source_rel,
                        "applied": applied,
                        "skipped_reason": None if applied else "already applied",
                    }
                )
                continue

            # 既存 slot がパイプに入っていたら拒否
            if slot["status"] in LOCKED_SLOT_STATUSES_FOR_RESERVE:
                die(
                    f"slot {slot_name} は status={slot['status']} のため予約反映不可。"
                    f"既に投稿パイプに入っています。reservations/{date}_{slot_name}.md を確認してください。"
                )

            # ファイル読み込み + ヘッダ書き換え
            text = path.read_text(encoding="utf-8")
            existing_ids = [c["id"] for c in state.get("candidates", [])]
            next_id = (max(existing_ids) + 1) if existing_ids else 1

            new_text, _changed = rewrite_header_id(text, next_id)
            if new_text != text:
                path.write_text(new_text, encoding="utf-8")

            # 本文（ヘッダ以降）の theme と hash
            body_after_header = PLACEHOLDER_HEADER_RE.sub("", new_text, count=1).strip()
            theme = extract_theme(body_after_header) or f"reservation_{slot_name}"
            content_hash = compute_content_hash(body_after_header)

            candidate = {
                "id": next_id,
                "type": "reservation",
                "format": "自由",
                "intensity": "自由",
                "theme": theme,
                "content_hash": content_hash,
                "status": "approved",
                "source": source_rel,
                "reservation_slot": slot_name,
                "revisions": [],
            }
            state.setdefault("candidates", []).append(candidate)

            slot["status"] = "reserved"
            slot["candidate_id"] = next_id
            slot["error"] = None

            state.setdefault("history", []).append(
                {
                    "ts": _ts(),
                    "event": "reservation_applied",
                    "actor": "apply_reservations.py",
                    "slot": slot_name,
                    "candidate_id": next_id,
                    "source": source_rel,
                    "theme": theme,
                    "by": actor,
                }
            )

            results.append(
                {
                    "slot": slot_name,
                    "candidate_id": next_id,
                    "theme": theme,
                    "source": source_rel,
                    "applied": True,
                    "skipped_reason": None,
                }
            )
            log(f"reservation applied: slot={slot_name} candidate_id={next_id} theme={theme!r}")

        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="reservations/ を state.json に反映")
    parser.add_argument("--date", default=datetime.now(JST).strftime("%Y-%m-%d"))
    parser.add_argument(
        "--actor",
        default=os.environ.get("STATE_UPDATE_ACTOR", "cli"),
        help="history に記録する actor 名（デフォルト: cli）",
    )
    args = parser.parse_args()

    results = apply_reservations_for_date(args.date, actor=args.actor)
    if not results:
        log(f"no reservations to apply for {args.date}")
        return
    for r in results:
        marker = "+" if r["applied"] else "="
        log(f"  [{marker}] {r['slot']}: candidate_id={r['candidate_id']} theme={r['theme'][:40]!r}")


if __name__ == "__main__":
    main()
