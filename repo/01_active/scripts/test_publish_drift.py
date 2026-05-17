#!/usr/bin/env python3
"""publish.py extract_post_section のヘッダ drift 診断テスト。

ファイル ↔ state の id ドリフトが起きたとき、エラーメッセージに
診断情報（observed ids / 推奨復旧コマンド）が含まれることを検証する。
"""

import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import publish


class DieCalled(Exception):
    """publish.die() の代替例外。die メッセージを保持する。"""
    def __init__(self, msg: str):
        super().__init__(msg)
        self.msg = msg


def patch_die():
    """publish.die を例外に置換。テスト終了時に restore_die() で戻す。"""
    original = publish.die

    def raising_die(msg: str, code: int = 1) -> None:
        raise DieCalled(msg)

    publish.die = raising_die
    return original


def restore_die(original):
    publish.die = original


def write_post(tmp: Path, content: str) -> Path:
    p = tmp / "post.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_detect_returns_ids_including_tbd():
    text = (
        "## 案1：A\nbody1\n"
        "## 案<TBD>：🗓️ reservation\nreserved body\n"
        "## 案3：C\nbody3\n"
    )
    ids = publish._detect_post_section_headers(text)
    assert ids == ["1", "<TBD>", "3"], f"FAIL: got {ids}"
    print("PASS test_detect_returns_ids_including_tbd")


def test_detect_returns_empty_for_no_headers():
    ids = publish._detect_post_section_headers("just body, no headers")
    assert ids == [], f"FAIL: got {ids}"
    print("PASS test_detect_returns_empty_for_no_headers")


def test_extract_returns_section_when_found():
    """既存挙動の保護: 正しいヘッダがあれば section を返す。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        post = write_post(tmp, "## 案7：title\nbody\n## 案8：x\n")
        section = publish.extract_post_section(post, 7)
        assert "案7" in section and "body" in section, f"FAIL: section={section!r}"
        assert "案8" not in section, f"FAIL: section bled into 案8: {section!r}"
        print("PASS test_extract_returns_section_when_found")


def test_die_message_for_tbd_placeholder():
    """ファイルが <TBD> ヘッダのままなら、apply_reservations.py での復旧を案内する。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        post = write_post(
            tmp,
            "## 案<TBD>：🗓️ reservation ／ 形式：自由 ／ 断定強度：自由\n\n"
            "【テーマ】test\n\n【1/1】\nbody\n",
        )
        original = patch_die()
        try:
            try:
                publish.extract_post_section(post, 7)
            except DieCalled as e:
                msg = e.msg
            else:
                raise AssertionError("FAIL: did not die")
        finally:
            restore_die(original)

        assert "案7" in msg, f"FAIL: expected id missing: {msg}"
        assert "<TBD>" in msg, f"FAIL: <TBD> diagnostic missing: {msg}"
        assert "apply_reservations" in msg, \
            f"FAIL: recovery hint missing: {msg}"
        print("PASS test_die_message_for_tbd_placeholder")


def test_die_message_for_id_drift():
    """state が 7 を期待しているがファイルに 1〜5 しかない場合、両方の id を出す。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        post = write_post(
            tmp,
            "## 案1：A\nbody1\n"
            "## 案2：B\nbody2\n"
            "## 案3：C\nbody3\n",
        )
        original = patch_die()
        try:
            try:
                publish.extract_post_section(post, 7)
            except DieCalled as e:
                msg = e.msg
            else:
                raise AssertionError("FAIL: did not die")
        finally:
            restore_die(original)

        assert "案7" in msg, f"FAIL: expected id missing: {msg}"
        assert "1" in msg and "2" in msg and "3" in msg, \
            f"FAIL: observed ids missing: {msg}"
        print("PASS test_die_message_for_id_drift")


def test_die_message_for_empty_file():
    """ヘッダがひとつも無い場合、フォーマット破損として案内する。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        post = write_post(tmp, "no headers at all\njust body\n")
        original = patch_die()
        try:
            try:
                publish.extract_post_section(post, 7)
            except DieCalled as e:
                msg = e.msg
            else:
                raise AssertionError("FAIL: did not die")
        finally:
            restore_die(original)

        assert "案7" in msg, f"FAIL: expected id missing: {msg}"
        assert ("ヘッダ" in msg or "header" in msg.lower()), \
            f"FAIL: format hint missing: {msg}"
        print("PASS test_die_message_for_empty_file")


if __name__ == "__main__":
    tests = [
        test_detect_returns_ids_including_tbd,
        test_detect_returns_empty_for_no_headers,
        test_extract_returns_section_when_found,
        test_die_message_for_tbd_placeholder,
        test_die_message_for_id_drift,
        test_die_message_for_empty_file,
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
