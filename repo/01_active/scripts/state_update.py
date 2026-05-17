#!/usr/bin/env python3
"""state.json 更新 CLI（Phase 3 / 7b-1 / 7d 部分承認 + 非破壊更新）

承認・却下・スキップを 1 つのスクリプトで完結させる。
Discord Bot からも CLI からも同じインターフェイスで叩ける共通 API。

Usage:
  state_update.py --date YYYY-MM-DD --select 1,3,5 [--message "..."] [--message-id ...]
  state_update.py --date YYYY-MM-DD --select-slot evening=5 [--select-slot morning=1] ...
  state_update.py --date YYYY-MM-DD --skip-slot morning [--skip-slot noon] ...
  state_update.py --date YYYY-MM-DD --select-slot evening=2 --skip-slot morning ...
  state_update.py --date YYYY-MM-DD --reject [--message "全部やり直して"]
  state_update.py --date YYYY-MM-DD --skip [--message "今日休む"]

設計:
- --select: 3 個の案 ID を指定。ids[0]→morning, ids[1]→noon, ids[2]→evening に割当（一括承認）
- --select-slot SLOT=ID: 部分スロット承認。SLOT は morning|noon|evening、ID は 1〜5。
  複数指定可。**指定されなかった slot は一切触らない**（時差承認に対応）。
  既に publishing/published/failed/partially_published な slot を指定したら拒否。
- --skip-slot SLOT: 特定 slot を明示的に skipped にする（candidate_id クリア + error="explicitly skipped"）。
  複数指定可。--select-slot との併用可。LOCKED slot は触らない（拒否）。
- --select-slot と --skip-slot は同じ呼び出しで併用可（ただし同じ slot を両方には指定できない）
- selected_ids は呼び出し後の slot 状態から再計算（candidate_id がセットされ skipped でない slot を朝→昼→夜順）
- --reject: status=rejected。再生成は別フロー（7c）でハンドル
- --skip: 全 pending slot を skipped、status=skipped
- 既に publishing/published な slot は触らない（再実行安全）
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ACTIVE_DIR = SCRIPT_DIR.parent
JST = timezone(timedelta(hours=9))


def _ts() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{_ts()}] [state_update.py] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


def load_state(state_path: Path) -> dict:
    if not state_path.is_file():
        die(f"state file not found: {state_path}")
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state: dict, state_path: Path) -> None:
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_ids(spec: str) -> list[int]:
    """\"1,3,5\" or \"1, 3, 5\" or \"1 3 5\" を [1,3,5] に"""
    raw = spec.replace(",", " ").replace("、", " ").replace("，", " ")
    try:
        ids = [int(x) for x in raw.split() if x.strip()]
    except ValueError:
        die(f"--select の値が数値ではありません: {spec}")
    return ids


def cmd_select(state_path: Path, ids: list[int], message: str | None, message_id: str | None, actor: str) -> None:
    state = load_state(state_path)

    # 検証
    if len(ids) != 3:
        die(f"--select は 3 個必要（指定: {ids} → {len(ids)} 個）")
    if len(set(ids)) != 3:
        die(f"--select に重複あり: {ids}")
    if not all(1 <= i <= 5 for i in ids):
        die(f"--select は 1〜5 の範囲: {ids}")

    cand_by_id = {c["id"]: c for c in state["candidates"]}
    for i in ids:
        if i not in cand_by_id:
            die(f"candidate id={i} が state に存在しない")

    slots_by_name = {s["slot"]: s for s in state["slots"]}
    for slot_name in ("morning", "noon", "evening"):
        if slot_name not in slots_by_name:
            die(f"slot {slot_name} が state に存在しない")

    # 予約済みスロットがあれば hard die（事故防止）
    slot_order = ("morning", "noon", "evening")
    reserved_slots = [
        slot_name for slot_name in slot_order
        if slots_by_name[slot_name]["status"] == "reserved"
    ]
    if reserved_slots:
        die(
            f"slot {reserved_slots} が予約済みのため --select で上書き不可"
            f"。先に reservations/ から該当ファイルを削除するか、--select-slot で残スロットだけ指定してください"
        )

    # 反映
    for cand_id in ids:
        cand_by_id[cand_id]["status"] = "approved"

    for slot_name, cand_id in zip(slot_order, ids):
        slot = slots_by_name[slot_name]
        if slot["status"] in ("publishing", "published", "failed", "partially_published"):
            log(f"WARN: slot {slot_name} status={slot['status']} (candidate_id 上書きしますが status は変更しません)")
        else:
            slot["status"] = "pending"
        slot["candidate_id"] = cand_id

    state["selected_ids"] = ids
    state["approval"] = {
        "approved": True,
        "approved_at": _ts(),
        "approved_by": actor,
        "approval_message_id": message_id,
        "approval_text": message,
    }
    state["status"] = "approved"

    state.setdefault("history", []).append({
        "ts": _ts(),
        "event": "approved",
        "actor": "state_update.py",
        "selected_ids": ids,
        "approval_text": message,
        "approval_message_id": message_id,
        "by": actor,
    })

    save_state(state, state_path)
    log(f"approved: {ids} (morning={ids[0]}, noon={ids[1]}, evening={ids[2]})")


SLOT_ORDER = ("morning", "noon", "evening")
# "reserved" は add_reservation.py / apply_reservations.py が予約スロットに付与する状態。
# 5 案承認フロー（--select / --select-slot / --skip-slot）からは上書き不可（事故防止）。
# 解除は reservations/<date>_<slot>.md を rm（または add_reservation --force で上書き）する運用。
LOCKED_SLOT_STATUSES = (
    "publishing",
    "published",
    "failed",
    "partially_published",
    "reserved",
)


def parse_slot_specs(specs: list[str]) -> dict[str, int]:
    """[\"evening=5\", \"morning=1\"] -> {\"evening\": 5, \"morning\": 1}"""
    result: dict[str, int] = {}
    for spec in specs:
        if "=" not in spec:
            die(f"--select-slot は SLOT=ID 形式: {spec!r}")
        slot, raw_id = spec.split("=", 1)
        slot = slot.strip().lower()
        raw_id = raw_id.strip()
        if slot not in SLOT_ORDER:
            die(f"--select-slot の SLOT は {SLOT_ORDER} のいずれか: {spec!r}")
        if slot in result:
            die(f"--select-slot に同じスロットが複数: {slot}")
        try:
            cand_id = int(raw_id)
        except ValueError:
            die(f"--select-slot の ID が数値でない: {spec!r}")
        if cand_id < 1:
            die(f"--select-slot の ID は 1 以上: {spec!r}")
        # 1〜5 は generate.sh の 5 案、6 以降は post-drafter エージェントの seed。
        # 実在チェックは cmd_modify_slots 側で candidates を見て行う。
        result[slot] = cand_id
    return result


def parse_skip_slot_specs(specs: list[str]) -> list[str]:
    """[\"morning\", \"evening\"] -> [\"morning\", \"evening\"]（順序維持・重複拒否）"""
    result: list[str] = []
    for spec in specs:
        slot = spec.strip().lower()
        if slot not in SLOT_ORDER:
            die(f"--skip-slot の SLOT は {SLOT_ORDER} のいずれか: {spec!r}")
        if slot in result:
            die(f"--skip-slot に同じスロットが複数: {slot}")
        result.append(slot)
    return result


def _recompute_selected_ids(slots_by_name: dict) -> list[int]:
    """current slot 状態から selected_ids を再計算。
    candidate_id がセットされ status が skipped でない slot を朝→昼→夜順で。"""
    out: list[int] = []
    for slot_name in SLOT_ORDER:
        slot = slots_by_name[slot_name]
        if slot.get("candidate_id") is None:
            continue
        if slot.get("status") == "skipped":
            continue
        out.append(slot["candidate_id"])
    return out


def cmd_modify_slots(
    state_path: Path,
    slot_to_id: dict[str, int],
    skip_slots: list[str],
    message: str | None,
    message_id: str | None,
    actor: str,
) -> None:
    """部分スロット承認 + 明示スキップ（非破壊・累積更新）。

    指定された slot のみ更新する。指定されなかった slot は一切触らない。
    時差承認（午前に noon 承認 → 午後に evening 追加）でも、既存 slot は壊れない。
    """
    if not slot_to_id and not skip_slots:
        die("--select-slot か --skip-slot のいずれかを 1 つ以上指定")

    overlap = set(slot_to_id) & set(skip_slots)
    if overlap:
        die(f"--select-slot と --skip-slot で同じ slot を指定: {sorted(overlap)}")

    state = load_state(state_path)

    cand_by_id = {c["id"]: c for c in state["candidates"]}
    for cand_id in slot_to_id.values():
        if cand_id not in cand_by_id:
            die(f"candidate id={cand_id} が state に存在しない")

    slots_by_name = {s["slot"]: s for s in state["slots"]}
    for slot_name in SLOT_ORDER:
        if slot_name not in slots_by_name:
            die(f"slot {slot_name} が state に存在しない")

    # ロック済 slot を更新しようとしたら拒否
    for slot_name in slot_to_id:
        slot = slots_by_name[slot_name]
        if slot["status"] in LOCKED_SLOT_STATUSES:
            die(
                f"slot {slot_name} は status={slot['status']} のため上書き不可"
                f"（既に投稿済 / 投稿中 / 失敗確定 / 予約済み）"
            )
    for slot_name in skip_slots:
        slot = slots_by_name[slot_name]
        if slot["status"] in LOCKED_SLOT_STATUSES:
            die(
                f"slot {slot_name} は status={slot['status']} のため skip 不可"
                f"（既に投稿済 / 投稿中 / 失敗確定 / 予約済み）"
            )

    # --select-slot 反映：指定 candidate を approved に、指定 slot を pending に
    for cand_id in slot_to_id.values():
        cand_by_id[cand_id]["status"] = "approved"
    for slot_name, cand_id in slot_to_id.items():
        slot = slots_by_name[slot_name]
        slot["status"] = "pending"
        slot["candidate_id"] = cand_id
        slot["error"] = None

    # --skip-slot 反映：指定 slot を skipped + candidate_id クリア
    for slot_name in skip_slots:
        slot = slots_by_name[slot_name]
        slot["status"] = "skipped"
        slot["error"] = "explicitly skipped"
        slot["candidate_id"] = None

    # selected_ids を現在の slot 状態から再計算
    state["selected_ids"] = _recompute_selected_ids(slots_by_name)

    # --select-slot が含まれていれば approval を立て直す
    # （--skip-slot 単独の場合は approval は触らない＝既存の承認状態を維持）
    if slot_to_id:
        state["approval"] = {
            "approved": True,
            "approved_at": _ts(),
            "approved_by": actor,
            "approval_message_id": message_id,
            "approval_text": message,
        }
        state["status"] = "approved"

    state.setdefault("history", []).append({
        "ts": _ts(),
        "event": "slots_modified",
        "actor": "state_update.py",
        "selected_slots": dict(slot_to_id),
        "skipped_slots": list(skip_slots),
        "selected_ids_after": list(state["selected_ids"]),
        "approval_text": message,
        "approval_message_id": message_id,
        "by": actor,
    })

    save_state(state, state_path)
    sel_desc = ", ".join(f"{s}={slot_to_id[s]}" for s in SLOT_ORDER if s in slot_to_id) or "(none)"
    skip_desc = ", ".join(skip_slots) if skip_slots else "(none)"
    log(f"slots modified: select=[{sel_desc}] skip=[{skip_desc}] selected_ids={state['selected_ids']}")


def cmd_reject(state_path: Path, message: str | None, message_id: str | None, actor: str) -> None:
    state = load_state(state_path)
    state["status"] = "rejected"
    state["approval"]["approved"] = False
    state.setdefault("history", []).append({
        "ts": _ts(),
        "event": "rejected",
        "actor": "state_update.py",
        "reason": message,
        "message_id": message_id,
        "by": actor,
    })
    save_state(state, state_path)
    log(f"rejected: {message or '(no reason)'}")


def cmd_skip(state_path: Path, message: str | None, message_id: str | None, actor: str) -> None:
    state = load_state(state_path)
    state["status"] = "skipped"
    state["approval"]["approved"] = False
    skipped_slots = []
    for slot in state["slots"]:
        if slot["status"] == "pending":
            slot["status"] = "skipped"
            slot["error"] = "skipped by user"
            skipped_slots.append(slot["slot"])
    state.setdefault("history", []).append({
        "ts": _ts(),
        "event": "skipped",
        "actor": "state_update.py",
        "reason": message,
        "message_id": message_id,
        "by": actor,
        "skipped_slots": skipped_slots,
    })
    save_state(state, state_path)
    log(f"skipped: {skipped_slots} ({message or '(no reason)'})")


def main() -> None:
    parser = argparse.ArgumentParser(description="state.json 更新 CLI")
    parser.add_argument("--date", default=datetime.now(JST).strftime("%Y-%m-%d"))
    parser.add_argument("--actor", default=os.environ.get("STATE_UPDATE_ACTOR", "cli"),
                        help="approved_by に記録（デフォルト: cli or env STATE_UPDATE_ACTOR）")
    parser.add_argument("--message", help="承認/却下メッセージ本文を記録")
    parser.add_argument("--message-id", help="Discord メッセージ ID を記録")

    parser.add_argument("--select", metavar="IDS", help="3 案一括承認（例: 1,3,5）")
    parser.add_argument(
        "--select-slot",
        metavar="SLOT=ID",
        action="append",
        dest="select_slot",
        help="部分スロット承認（例: evening=5）。複数指定可、--skip-slot と併用可",
    )
    parser.add_argument(
        "--skip-slot",
        metavar="SLOT",
        action="append",
        dest="skip_slot",
        help="特定スロットを明示スキップ（例: morning）。複数指定可、--select-slot と併用可",
    )
    parser.add_argument("--reject", action="store_true", help="全案却下")
    parser.add_argument("--skip", action="store_true", help="今日は投稿スキップ")

    args = parser.parse_args()

    state_path = ACTIVE_DIR / "state" / f"{args.date}.json"

    # コマンドクラス判定（同時に複数クラスは不可）
    classes = []
    if args.select:
        classes.append("select")
    if args.select_slot or args.skip_slot:
        classes.append("slot_modify")
    if args.reject:
        classes.append("reject")
    if args.skip:
        classes.append("skip")
    if not classes:
        die("--select / --select-slot / --skip-slot / --reject / --skip のいずれかが必須")
    if len(classes) > 1:
        die(f"複数のコマンドを同時指定できません: {classes}")

    cls = classes[0]
    if cls == "select":
        ids = parse_ids(args.select)
        cmd_select(state_path, ids, args.message, args.message_id, args.actor)
    elif cls == "slot_modify":
        slot_to_id = parse_slot_specs(args.select_slot or [])
        skip_slots = parse_skip_slot_specs(args.skip_slot or [])
        cmd_modify_slots(state_path, slot_to_id, skip_slots, args.message, args.message_id, args.actor)
    elif cls == "reject":
        cmd_reject(state_path, args.message, args.message_id, args.actor)
    elif cls == "skip":
        cmd_skip(state_path, args.message, args.message_id, args.actor)


if __name__ == "__main__":
    main()
