#!/usr/bin/env python3
"""add_reservation.py / add_seed.py の body サニタイズ防御テスト。

2026-05-15 朝の予約投稿が `post body not extracted (neither 【N/M】 nor 【テーマ】
found)` で失敗した事故の再発防止。

事故の経緯:
- post-drafter の Swap モード（手順 7-B-2）は、seed ファイルから本文を抽出
  するときに先頭の `## 案N：🌱 seed-... ` ヘッダ行を剥がす責務がある
- だが LLM 側がこれを忘れて `## 案16：🌱 seed-polish ...` 込みの body を
  add_reservation.py に渡した
- add_reservation.py は build_reservation_text() で `## 案<TBD>：🗓️ reservation ...`
  を頭にくっつけたため、ファイルが二重ヘッダになった
- publish.py の extract_post_section が `^## 案<id>：.+?(?=^## 案\\d+：|\\Z)`
  で切ったとき、最初のヘッダから二番目のヘッダ直前まで（メタコメントのみ）
  しか拾えず、本文が見つからず die

防御: CLI 入口（add_reservation.py / add_seed.py 両方）で、--body-file の
先頭に並ぶ `## ` ヘッダ行を全て剥がす。
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import add_reservation as ar
import add_seed as asd


# ---------------------------------------------------------------------------
# add_reservation.strip_leading_markdown_headers
# ---------------------------------------------------------------------------

def test_ar_strips_single_seed_header():
    """post-drafter が seed の `## 案N：🌱 seed-polish ...` を剥がし忘れた
    body を渡してきたら、CLI 側で剥がす（実際の 2026-05-15 事故の入力）。"""
    body = (
        "## 案16：🌱 seed-polish ／ 形式：自由 ／ 断定強度：自由\n"
        "\n"
        "【テーマ】間接プロンプトインジェクション\n"
        "\n"
        "【1/2】本文1\n"
        "【2/2】本文2\n"
    )
    out, stripped = ar.strip_leading_markdown_headers(body)
    assert stripped == ["## 案16：🌱 seed-polish ／ 形式：自由 ／ 断定強度：自由"], \
        f"FAIL: stripped={stripped!r}"
    assert out.startswith("【テーマ】間接プロンプトインジェクション"), \
        f"FAIL: out start = {out[:50]!r}"
    assert "## 案16" not in out, f"FAIL: header leaked into body:\n{out}"
    print("PASS test_ar_strips_single_seed_header")


def test_ar_no_headers_unchanged():
    """body が正規フォーマット（`【テーマ】...` で始まる）なら何も剥がさない。"""
    body = "【テーマ】テスト\n\n本文\n"
    out, stripped = ar.strip_leading_markdown_headers(body)
    assert stripped == [], f"FAIL: unexpected strip {stripped!r}"
    assert out == body.strip(), f"FAIL: body altered:\n{out!r}"
    print("PASS test_ar_no_headers_unchanged")


def test_ar_strips_multiple_stacked_headers():
    """ヘッダが複数連続している病的ケース（agent が二段重ねた場合）も全部剥がす。"""
    body = (
        "## 案16：🌱 seed-polish\n"
        "\n"
        "## 案7：🌱 seed-polish\n"
        "\n"
        "【テーマ】本文\n"
    )
    out, stripped = ar.strip_leading_markdown_headers(body)
    assert len(stripped) == 2, f"FAIL: expected 2 stripped, got {len(stripped)}"
    assert out.startswith("【テーマ】"), f"FAIL: out={out[:50]!r}"
    print("PASS test_ar_strips_multiple_stacked_headers")


def test_ar_does_not_strip_body_with_no_blank_after_header():
    """body 中に `## ` で始まる行があっても、先頭でなければ剥がさない
    （Threads 本文中に `##` が出ることは想定しないが、念のため非破壊）。"""
    body = "【テーマ】テスト\n\n本文に ## 案 みたいな文字列が出てきても剥がさない\n"
    out, stripped = ar.strip_leading_markdown_headers(body)
    assert stripped == [], f"FAIL: stripped mid-body {stripped!r}"
    assert "## 案" in out
    print("PASS test_ar_does_not_strip_body_with_no_blank_after_header")


def test_ar_handles_empty_after_strip():
    """ヘッダしか無い body は、剥がした後に空になる。main() 側で die される
    ことを期待するので、ここでは空文字列が返ることだけ確認。"""
    body = "## 案16：🌱 seed-polish\n\n"
    out, stripped = ar.strip_leading_markdown_headers(body)
    assert stripped == ["## 案16：🌱 seed-polish"], f"FAIL: stripped={stripped!r}"
    assert out == "", f"FAIL: expected empty, got {out!r}"
    print("PASS test_ar_handles_empty_after_strip")


def test_ar_preserves_inner_text_format():
    """ヘッダを剥がしたあとの本文の `【N/M】` ブロック構造が壊れていないこと
    （publish.py の extract_thread_parts が再度パースできる形を維持）。"""
    body = (
        "## 案16：🌱 seed-polish ／ 形式：自由 ／ 断定強度：自由\n"
        "\n"
        "【テーマ】テスト\n"
        "\n"
        "【1/2】最初の投稿\n"
        "数字: 123\n"
        "\n"
        "【2/2】参考↓\n"
        "https://example.com\n"
    )
    out, _ = ar.strip_leading_markdown_headers(body)
    assert "【1/2】最初の投稿" in out
    assert "【2/2】参考↓" in out
    assert "https://example.com" in out
    print("PASS test_ar_preserves_inner_text_format")


# ---------------------------------------------------------------------------
# add_seed.strip_leading_markdown_headers（同じ仕様）
# ---------------------------------------------------------------------------

def test_asd_strips_seed_header_iteration_case():
    """post-drafter のイテレーション（既存 seed への修正指示）で、agent が
    parent seed の `## 案N：🌱 seed-polish ...` を剥がし忘れた body を渡し
    てきたら、add_seed.py 側で剥がす。"""
    body = (
        "## 案9：🌱 seed-polish ／ 形式：自由 ／ 断定強度：自由\n"
        "\n"
        "【テーマ】既存 seed を更新\n"
        "\n"
        "更新後の本文\n"
    )
    out, stripped = asd.strip_leading_markdown_headers(body)
    assert stripped == ["## 案9：🌱 seed-polish ／ 形式：自由 ／ 断定強度：自由"]
    assert out.startswith("【テーマ】既存 seed を更新")
    print("PASS test_asd_strips_seed_header_iteration_case")


def test_asd_no_headers_unchanged():
    body = "【テーマ】テスト\n\n本文\n"
    out, stripped = asd.strip_leading_markdown_headers(body)
    assert stripped == []
    assert out == body.strip()
    print("PASS test_asd_no_headers_unchanged")


# ---------------------------------------------------------------------------
# build_reservation_text と組み合わせた E2E（実際の事故シナリオの再現）
# ---------------------------------------------------------------------------

def test_e2e_no_double_header_after_sanitization():
    """post-drafter が seed ヘッダ込みの body を渡してきても、サニタイズ後に
    build_reservation_text を通したら、結果ファイルに `## ` ヘッダがちょうど
    1 つしか存在しないこと（publish.py の extract_post_section が壊れない）。"""
    raw_body = (
        "## 案16：🌱 seed-polish ／ 形式：自由 ／ 断定強度：自由\n"
        "\n"
        "【テーマ】間接プロンプトインジェクション\n"
        "\n"
        "【1/2】本文1\n"
        "\n"
        "【2/2】参考↓\nhttps://example.com\n"
    )
    sanitized, stripped = ar.strip_leading_markdown_headers(raw_body)
    assert stripped, "FAIL: ヘッダが剥がされていない"

    text = ar.build_reservation_text(
        sanitized,
        message_id="test_msg_id",
        theme="間接プロンプトインジェクション",
        actor="discord",
    )

    # 結果ファイルに `## ` 行は予約ヘッダの 1 行だけのはず
    header_lines = [ln for ln in text.splitlines() if ln.startswith("## ")]
    assert len(header_lines) == 1, \
        f"FAIL: expected 1 `## ` header, got {len(header_lines)}:\n{header_lines!r}"
    assert "🗓️ reservation" in header_lines[0], \
        f"FAIL: header is not reservation header: {header_lines[0]!r}"

    # publish.py の extract_post_section が探す本文マーカーが本文側に残ってる
    assert "【1/2】本文1" in text
    assert "【テーマ】間接プロンプトインジェクション" in text
    print("PASS test_e2e_no_double_header_after_sanitization")


if __name__ == "__main__":
    tests = [
        test_ar_strips_single_seed_header,
        test_ar_no_headers_unchanged,
        test_ar_strips_multiple_stacked_headers,
        test_ar_does_not_strip_body_with_no_blank_after_header,
        test_ar_handles_empty_after_strip,
        test_ar_preserves_inner_text_format,
        test_asd_strips_seed_header_iteration_case,
        test_asd_no_headers_unchanged,
        test_e2e_no_double_header_after_sanitization,
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
