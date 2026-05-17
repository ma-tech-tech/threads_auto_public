#!/usr/bin/env python3
"""apply_reservations.py の冪等パス再同期バグの単体テスト。

バグ: add_reservation.py --force で reservations/<date>_<slot>.md が
`## 案<TBD>：` ヘッダで上書きされた後、apply_reservations_for_date() が
冪等パス（同じ source の candidate が既に state にある）に入ると
ヘッダ書き換えをスキップしてしまい、publish.py が「案N not found」で死ぬ。

このテストは ACTIVE_DIR をモンキーパッチして tmp dir で完結させる。
"""

import hashlib
import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import apply_reservations as ar


def hash_body(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def make_state(date: str, candidates: list, slots: list) -> dict:
    return {
        "schema_version": 1,
        "date": date,
        "status": "approved",
        "candidates": candidates,
        "selected_ids": [],
        "approval": {"approved": False},
        "slots": slots,
        "history": [],
    }


def setup_fixture(tmp: Path, *, date: str, cid: int, body: str, file_header_id: str):
    """tmp dir に state.json + reservation file を用意。

    file_header_id: 予約ファイル先頭の `## 案<XXX>：` の XXX 部分（"<TBD>" or "7" 等）
    """
    active = tmp / "01_active"
    (active / "state").mkdir(parents=True)
    (active / "reservations").mkdir(parents=True)

    ar.ACTIVE_DIR = active

    source_rel = f"reservations/{date}_evening.md"
    candidate = {
        "id": cid,
        "type": "reservation",
        "format": "自由",
        "intensity": "自由",
        "theme": "test theme",
        "content_hash": hash_body(body),
        "status": "approved",
        "source": source_rel,
        "reservation_slot": "evening",
        "revisions": [],
    }
    slots = [
        {"slot": "morning", "scheduled_at": f"{date}T07:00:00+09:00",
         "candidate_id": None, "status": "pending", "profile": None,
         "parent_id": None, "reply_ids": [], "permalink": None,
         "published_at": None, "retry_count": 1, "error": None, "metrics_24h": None},
        {"slot": "noon", "scheduled_at": f"{date}T12:00:00+09:00",
         "candidate_id": None, "status": "pending", "profile": None,
         "parent_id": None, "reply_ids": [], "permalink": None,
         "published_at": None, "retry_count": 1, "error": None, "metrics_24h": None},
        {"slot": "evening", "scheduled_at": f"{date}T20:00:00+09:00",
         "candidate_id": cid, "status": "reserved", "profile": None,
         "parent_id": None, "reply_ids": [], "permalink": None,
         "published_at": None, "retry_count": 1, "error": None, "metrics_24h": None},
    ]
    state = make_state(date, [candidate], slots)
    (active / "state" / f"{date}.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    f = active / "reservations" / f"{date}_evening.md"
    f.write_text(
        f"## 案{file_header_id}：🗓️ reservation ／ 形式：自由 ／ 断定強度：自由\n"
        f"<!-- reservation_created_at: 2099-01-01T11:00:00+09:00 -->\n"
        f"\n{body}\n",
        encoding="utf-8",
    )
    return active, f


def test_idempotent_path_rewrites_stale_tbd_header():
    """既存 candidate と一致する source の予約ファイルが <TBD> ヘッダの場合、
    冪等パスで案N に書き換える。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        date = "2099-01-01"
        cid = 7
        body = "【テーマ】test theme\n\n【1/1】\nbody text"

        active, f = setup_fixture(tmp, date=date, cid=cid, body=body, file_header_id="<TBD>")

        results = ar.apply_reservations_for_date(date, actor="test")

        new_content = f.read_text(encoding="utf-8")
        first_line = new_content.split("\n", 1)[0]
        assert f"## 案{cid}：" in first_line, \
            f"FAIL: header not rewritten. first_line={first_line!r}"
        assert "<TBD>" not in first_line, \
            f"FAIL: <TBD> still present. first_line={first_line!r}"
        assert results and results[0]["slot"] == "evening", \
            f"FAIL: results={results!r}"
        print(f"PASS test_idempotent_path_rewrites_stale_tbd_header: header={first_line!r}")


def test_idempotent_path_is_idempotent_after_rewrite():
    """2 回目以降の呼び出しではヘッダもファイルも変化しない。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        date = "2099-01-02"
        cid = 7
        body = "【テーマ】test theme\n\n【1/1】\nbody text"

        active, f = setup_fixture(tmp, date=date, cid=cid, body=body, file_header_id="<TBD>")

        ar.apply_reservations_for_date(date, actor="test")
        first_run_content = f.read_text(encoding="utf-8")

        ar.apply_reservations_for_date(date, actor="test")
        second_run_content = f.read_text(encoding="utf-8")

        assert first_run_content == second_run_content, \
            "FAIL: 2nd run changed file content"
        print("PASS test_idempotent_path_is_idempotent_after_rewrite")


def test_idempotent_path_already_correct_header_unchanged():
    """既にヘッダが案N の場合、ファイルは変更されない。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        date = "2099-01-03"
        cid = 7
        body = "【テーマ】test theme\n\n【1/1】\nbody text"

        active, f = setup_fixture(tmp, date=date, cid=cid, body=body, file_header_id="7")
        before = f.read_text(encoding="utf-8")

        ar.apply_reservations_for_date(date, actor="test")
        after = f.read_text(encoding="utf-8")

        assert before == after, "FAIL: file changed when header was already correct"
        print("PASS test_idempotent_path_already_correct_header_unchanged")


def test_idempotent_path_refreshes_content_hash_when_body_changed():
    """--force で本文が変わった場合、content_hash と theme が更新される。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        date = "2099-01-04"
        cid = 7
        old_body = "【テーマ】old theme\n\n【1/1】\nold body"
        new_body = "【テーマ】new theme\n\n【1/1】\nnew body"

        active, f = setup_fixture(tmp, date=date, cid=cid, body=old_body, file_header_id="<TBD>")
        f.write_text(
            f"## 案<TBD>：🗓️ reservation ／ 形式：自由 ／ 断定強度：自由\n"
            f"\n{new_body}\n",
            encoding="utf-8",
        )

        ar.apply_reservations_for_date(date, actor="test")

        state = json.loads((active / "state" / f"{date}.json").read_text(encoding="utf-8"))
        cand = next(c for c in state["candidates"] if c["id"] == cid)
        expected_hash = hash_body(
            f"\n{new_body}\n".strip()
        )
        assert cand["content_hash"] == expected_hash, \
            f"FAIL: content_hash not refreshed. got={cand['content_hash']}, expected={expected_hash}"
        assert cand["theme"] == "new theme", \
            f"FAIL: theme not refreshed. got={cand['theme']!r}"
        print("PASS test_idempotent_path_refreshes_content_hash_when_body_changed")


def test_idempotent_path_records_resync_history_when_applied():
    """冪等パスで resync が走った（applied=True）時、history に reservation_resynced が積まれる。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        date = "2099-01-05"
        cid = 7
        body = "【テーマ】test theme\n\n【1/1】\nbody text"

        active, f = setup_fixture(tmp, date=date, cid=cid, body=body, file_header_id="<TBD>")

        ar.apply_reservations_for_date(date, actor="test")

        state = json.loads((active / "state" / f"{date}.json").read_text(encoding="utf-8"))
        events = [h["event"] for h in state.get("history", [])]
        assert "reservation_resynced" in events, \
            f"FAIL: reservation_resynced not in history. events={events}"

        resync_event = next(h for h in state["history"] if h["event"] == "reservation_resynced")
        assert resync_event["candidate_id"] == cid, \
            f"FAIL: candidate_id mismatch in event: {resync_event}"
        assert resync_event["slot"] == "evening", \
            f"FAIL: slot mismatch: {resync_event}"
        assert resync_event["actor"] == "apply_reservations.py", \
            f"FAIL: actor mismatch: {resync_event}"
        assert resync_event["by"] == "test", \
            f"FAIL: by mismatch: {resync_event}"
        print("PASS test_idempotent_path_records_resync_history_when_applied")


def test_idempotent_path_no_history_when_already_clean():
    """既にヘッダ・hash が一致している場合、history に追加されない（無駄な記録を避ける）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        date = "2099-01-06"
        cid = 7
        body = "【テーマ】test theme\n\n【1/1】\nbody text"

        active, f = setup_fixture(tmp, date=date, cid=cid, body=body, file_header_id="<TBD>")

        ar.apply_reservations_for_date(date, actor="test")
        state_after_first = json.loads((active / "state" / f"{date}.json").read_text(encoding="utf-8"))
        history_len_after_first = len(state_after_first.get("history", []))

        ar.apply_reservations_for_date(date, actor="test")
        state_after_second = json.loads((active / "state" / f"{date}.json").read_text(encoding="utf-8"))
        history_len_after_second = len(state_after_second.get("history", []))

        assert history_len_after_first == history_len_after_second, \
            f"FAIL: 2nd run added history entries. " \
            f"before={history_len_after_first}, after={history_len_after_second}"
        print("PASS test_idempotent_path_no_history_when_already_clean")


if __name__ == "__main__":
    tests = [
        test_idempotent_path_rewrites_stale_tbd_header,
        test_idempotent_path_is_idempotent_after_rewrite,
        test_idempotent_path_already_correct_header_unchanged,
        test_idempotent_path_refreshes_content_hash_when_body_changed,
        test_idempotent_path_records_resync_history_when_applied,
        test_idempotent_path_no_history_when_already_clean,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n{failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\n{len(tests)}/{len(tests)} tests passed")
