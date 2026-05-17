#!/usr/bin/env python3
"""Threads 自動投稿スクリプト（Phase 3 / 6a+6b+6c+6d）

state/YYYY-MM-DD.json を読み、指定 slot を Threads Graph API で投稿する。
6d 範囲：API 呼び出し 3 回リトライ・Discord 成功/失敗通知・±15 分ランダム化・retry_count。
途中失敗時は partially_published で保存、再実行で未投稿分から再開する。

Usage:
  publish.py --slot morning [--date YYYY-MM-DD] [--profile test|production] [--dry-run] [--randomize]

設計判断:
- profile=test がデフォルト（事故防止）。production は明示指定が必要
- 冪等性: status==published なら何もしない
- 承認ゲート: approval.approved!=true なら投稿しない
- 本文抽出: posts/YYYY-MM-DD_5案.md から ## 案N： → 【1/N】〜【N/N】を順番に
- 連投: 全 reply は reply_to_id=parent_id（root）に向ける
- 再開: partially_published / publishing を検知したら parent_id + reply_ids から続行
- リトライ: 各 API 呼び出しに対し 3 回（1s/2s/4s backoff）
- 通知: 成功時に permalink を、失敗時にエラー詳細を Discord へ
- ランダム化: --randomize で 0〜900 秒のランダム sleep（launchd で発火時刻を分散）
"""

import argparse
import fcntl
import json
import os
import random
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

import requests


# --- パス・定数 ---
SCRIPT_DIR = Path(__file__).resolve().parent
ACTIVE_DIR = SCRIPT_DIR.parent              # repo/01_active/
PROJECT_ROOT = ACTIVE_DIR.parent.parent     # threads_auto/

EDITS_DIR = ACTIVE_DIR / "learning" / "edits"
REVISIONS_LOG_DIR = ACTIVE_DIR / "logs" / "revisions"
EMERGENCY_STOP_SENTINEL = ACTIVE_DIR / "state" / "EMERGENCY_STOP_PRODUCTION"

JST = timezone(timedelta(hours=9))
THREADS_API = "https://graph.threads.net/v1.0"

API_MAX_ATTEMPTS = 3
API_INITIAL_BACKOFF_SEC = 1.0
RANDOMIZE_MAX_SEC = 900  # 15 分


class ThreadsAPIError(Exception):
    """Threads API 呼び出しの失敗を表す。リトライ可能。"""
    def __init__(self, message: str, status_code: int | None = None, response_text: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


# --- ロギング ---
def _ts() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{_ts()}] [publish.py] {msg}"
    print(line, flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


def with_retry(func, op_name: str, max_attempts: int = API_MAX_ATTEMPTS, initial_backoff: float = API_INITIAL_BACKOFF_SEC):
    """ThreadsAPIError をリトライする。最終失敗で再 raise。"""
    last_err: ThreadsAPIError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except ThreadsAPIError as e:
            last_err = e
            log(f"  {op_name} attempt {attempt}/{max_attempts} failed: {e} (status={e.status_code})")
            if attempt < max_attempts:
                backoff = initial_backoff * (2 ** (attempt - 1))
                log(f"  retrying in {backoff:.1f}s...")
                time.sleep(backoff)
    assert last_err is not None
    raise last_err


# --- 環境変数 ---
def load_env() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def get_profile_creds(profile: str) -> tuple[str, str, str]:
    if profile == "production":
        token = os.environ.get("THREADS_ACCESS_TOKEN", "")
        user_id = os.environ.get("THREADS_USER_ID", "")
        username = os.environ.get("THREADS_USERNAME", "")
    elif profile == "test":
        token = os.environ.get("THREADS_TEST_ACCESS_TOKEN", "")
        user_id = os.environ.get("THREADS_TEST_USER_ID", "")
        username = os.environ.get("THREADS_TEST_USERNAME", "test_account")
    else:
        die(f"unknown profile: {profile}")
    if not token or not user_id:
        die(f"missing credentials for profile={profile} (THREADS_*ACCESS_TOKEN / *USER_ID required in .env)")
    return token, user_id, username


# --- state.json ---
def load_state(date: str) -> tuple[dict, Path]:
    path = ACTIVE_DIR / "state" / f"{date}.json"
    if not path.is_file():
        die(f"state file not found: {path}\nヒント: example.json をコピーして {date}.json を手動作成してください")
    state = json.loads(path.read_text(encoding="utf-8"))
    return state, path


# --- B2 案：ファイルロック付き atomic 更新 ---
# 並行 publish.py（朝/昼/夜の同時 kickstart 等）から state.json を更新する際、
# 「最後に書いた人が他 slot の published を上書きで消す」事故を防ぐ。
# 書き込みは必ず update_state_atomically 経由にし、ロック取得 → 最新を再読込 →
# mutator 適用 → 書き戻し → 解放 のサイクルを守る。

LOCK_TIMEOUT_SEC = 30
LOCK_POLL_INTERVAL_SEC = 0.1


@contextmanager
def state_lock(state_path: Path):
    """state.json に対する排他ロック（fcntl.flock）。
    .lock ファイルを介して取得。タイムアウトでデッドロック検知。"""
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


def update_state_atomically(state_path: Path, mutator: Callable[[dict], None]) -> dict:
    """ロック取得 → state 再読込 → mutator(state) → 書き戻し。
    戻り値は mutator 適用後の state（呼び出し元が in-memory ベースラインを更新するため）。"""
    with state_lock(state_path):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        mutator(state)
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return state


def _find_slot(state: dict, slot_name: str) -> dict:
    sl = next((s for s in state["slots"] if s["slot"] == slot_name), None)
    if not sl:
        raise KeyError(f"slot '{slot_name}' not in state")
    return sl


def save_state(state: dict, path: Path) -> None:
    """旧来の全体上書き。ロックなしの直書きなので、新規コードでは
    update_state_atomically を使うこと。レガシー互換のため残置。"""
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --- posts/ から本文抽出 ---
def _detect_post_section_headers(text: str) -> list[str]:
    """ファイル中の `## 案<id>：` ヘッダの id 一覧を出現順に返す（drift 診断用）。
    `<TBD>` プレースホルダも含める。"""
    return re.findall(r"^## 案(\d+|<TBD>)：", text, re.MULTILINE)


def extract_post_section(post_file: Path, candidate_id: int) -> str:
    text = post_file.read_text(encoding="utf-8")
    pattern = rf"^## 案{candidate_id}：.+?(?=^## 案\d+：|\Z)"
    m = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    if not m:
        observed = _detect_post_section_headers(text)
        if not observed:
            die(
                f"案{candidate_id} not found in {post_file}: "
                f"ファイルに `## 案N：` ヘッダがひとつも見つかりません。"
                f"ファイルが空か、フォーマットが破損している可能性があります"
            )
        elif "<TBD>" in observed:
            die(
                f"案{candidate_id} not found in {post_file}: "
                f"ファイルが `## 案<TBD>：` プレースホルダのまま固まっています "
                f"(observed={observed})。"
                f"`python3 scripts/apply_reservations.py --date <date>` で "
                f"ヘッダを再同期してください"
            )
        else:
            die(
                f"案{candidate_id} not found in {post_file}: "
                f"state は candidate_id={candidate_id} を期待していますが、"
                f"ファイルには案 {observed} しかありません (id ドリフト)。"
                f"state.candidates と posts/ の整合性を確認してください"
            )
    return m.group(0)


def extract_thread_parts(section: str) -> list[str]:
    """マルチスレッド案は【1/N】〜【N/N】を順番に、1本投稿案は単一要素のリストを返す。
    【自己採点】以降のメタ情報は投稿に含めない。"""
    matches = list(re.finditer(
        r"【(\d+)/(\d+)】\s*\n(.+?)(?=\n【\d+/\d+】|\n【自己採点】|\n【厚みチェック】|\Z)",
        section,
        re.DOTALL,
    ))
    if matches:
        ordered = sorted(matches, key=lambda m: int(m.group(1)))
        return [m.group(3).strip() for m in ordered]

    # 1本投稿: 【テーマ】見出し行の次行から【自己採点】の前まで
    m = re.search(
        r"【テーマ】[^\n]*\n\n(.+?)(?=\n【自己採点】|\n【厚みチェック】|\Z)",
        section,
        re.DOTALL,
    )
    if m:
        return [m.group(1).strip()]

    die("post body not extracted (neither 【N/M】 nor 【テーマ】 found)")


# --- Threads API ---
def _create_container_once(token: str, user_id: str, text: str, reply_to_id: str | None) -> str:
    url = f"{THREADS_API}/{user_id}/threads"
    params = {"media_type": "TEXT", "text": text, "access_token": token}
    if reply_to_id:
        params["reply_to_id"] = reply_to_id
    try:
        r = requests.post(url, data=params, timeout=30)
    except requests.RequestException as e:
        raise ThreadsAPIError(f"network error: {e}") from e
    if r.status_code >= 400:
        raise ThreadsAPIError(f"create_container HTTP {r.status_code}", status_code=r.status_code, response_text=r.text)
    container_id = r.json().get("id")
    if not container_id:
        raise ThreadsAPIError(f"create_container response missing id: {r.text}", status_code=r.status_code, response_text=r.text)
    return container_id


def create_container(token: str, user_id: str, text: str, reply_to_id: str | None = None, dry_run: bool = False) -> str:
    log(f"create_container: text_len={len(text)}, reply_to={reply_to_id or 'None'}")
    if dry_run:
        return f"DRY_CONTAINER_{int(time.time() * 1000)}"
    container_id = with_retry(
        lambda: _create_container_once(token, user_id, text, reply_to_id),
        op_name="create_container",
    )
    log(f"  -> container_id={container_id}")
    return container_id


def _publish_container_once(token: str, user_id: str, container_id: str) -> str:
    url = f"{THREADS_API}/{user_id}/threads_publish"
    params = {"creation_id": container_id, "access_token": token}
    try:
        r = requests.post(url, data=params, timeout=30)
    except requests.RequestException as e:
        raise ThreadsAPIError(f"network error: {e}") from e
    if r.status_code >= 400:
        raise ThreadsAPIError(f"publish_container HTTP {r.status_code}", status_code=r.status_code, response_text=r.text)
    media_id = r.json().get("id")
    if not media_id:
        raise ThreadsAPIError(f"publish_container response missing id: {r.text}", status_code=r.status_code, response_text=r.text)
    return media_id


def publish_container(token: str, user_id: str, container_id: str, dry_run: bool = False) -> str:
    log(f"publish_container: container_id={container_id}")
    if dry_run:
        return f"DRY_MEDIA_{int(time.time() * 1000)}"
    media_id = with_retry(
        lambda: _publish_container_once(token, user_id, container_id),
        op_name="publish_container",
    )
    log(f"  -> media_id={media_id}")
    return media_id


def get_permalink(token: str, media_id: str, dry_run: bool = False) -> str | None:
    if dry_run:
        return f"https://www.threads.com/dry-run/{media_id}"
    url = f"{THREADS_API}/{media_id}"
    params = {"fields": "permalink", "access_token": token}
    try:
        r = requests.get(url, params=params, timeout=30)
    except requests.RequestException as e:
        log(f"get_permalink network error (non-fatal): {e}")
        return None
    if r.status_code >= 400:
        log(f"get_permalink failed (non-fatal): {r.status_code} {r.text}")
        return None
    return r.json().get("permalink")


# --- 編集事例（learning/edits/）---
# publish 成功時に「初稿 → 修正履歴 → 決定版」の 3 段階を 1 ファイルに集約する。
# 初稿は revise.py log の最初の before_section から復元（修正がなければ現セクション）、
# 修正履歴は revise.py log の各 entry、決定版は実投稿された parts。
# Phase 2 で analyze_edits.py が読むため、Markdown 形式で書き出す。


def _load_revision_entries(date_str: str, candidate_id: int) -> list[dict]:
    """logs/revisions/*.jsonl から該当 (date, candidate_id) を ts 昇順で抽出。"""
    if not REVISIONS_LOG_DIR.is_dir():
        return []
    matched: list[dict] = []
    for path in sorted(REVISIONS_LOG_DIR.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("date") == date_str and entry.get("candidate_id") == candidate_id:
                matched.append(entry)
    matched.sort(key=lambda e: e.get("ts", ""))
    return matched


def _classify_channel(candidate: dict) -> tuple[str, str]:
    """candidate.type からチャンネル種別を導出。

    戻り値: (channel, source_type)
      - 5案系（A/B/C/D/E 型）          → ("threads_auto",         "5案")
      - seed系（type が "seed-" で始まる）→ ("threads_auto_drafts", "seed")
      - reservation                     → ("manual",              "reservation")
      - その他                          → ("unknown",             type そのまま or "unknown")
    """
    cand_type = (candidate.get("type") or "").strip()
    if cand_type.startswith("seed-"):
        return ("threads_auto_drafts", "seed")
    if cand_type == "reservation":
        return ("manual", "reservation")
    if cand_type and cand_type[0] in ("A", "B", "C", "D", "E"):
        return ("threads_auto", "5案")
    return ("unknown", cand_type or "unknown")


def _build_edit_case_md(
    *,
    state: dict,
    slot_name: str,
    candidate: dict,
    section: str,
    parts: list[str],
    profile: str,
    published_at: str,
    permalink: str | None,
    revisions: list[dict],
) -> str:
    date_str = state.get("date", "")
    candidate_id = candidate.get("id")
    cand_type = (candidate.get("type") or "").strip()
    cand_format = (candidate.get("format") or "").strip()
    intensity = (candidate.get("intensity") or "").strip()
    type_label_parts = [p for p in (cand_type, cand_format and f"形式:{cand_format}", intensity and f"断定強度:{intensity}") if p]
    type_label = "／".join(type_label_parts)
    source = candidate.get("source") or state.get("post_file") or ""
    channel, source_type = _classify_channel(candidate)

    out: list[str] = []
    out.append(f"# 編集事例: {date_str} {slot_name} #{candidate_id}")
    out.append("")
    out.append("## メタ")
    out.append("")
    out.append(f"- date: {date_str}")
    out.append(f"- slot: {slot_name}")
    out.append(f"- candidate_id: {candidate_id}")
    out.append(f"- channel: {channel}")
    out.append(f"- source_type: {source_type}")
    if type_label:
        out.append(f"- type: {type_label}")
    out.append(f"- source: {source}")
    out.append(f"- revision_count: {len(revisions)}")
    out.append(f"- published_at: {published_at}")
    out.append(f"- permalink: {permalink or ''}")
    out.append(f"- profile: {profile}")
    out.append("- diff_categories: []  # Phase 3 で手動/自動タグ付け")
    out.append("")

    # 初稿: 修正履歴があれば最初の before_section、なければ現在のセクション
    if revisions:
        first_draft = (revisions[0].get("before_section") or "").rstrip()
    else:
        first_draft = section.rstrip()
    out.append("## 初稿")
    out.append("")
    out.append(first_draft)
    out.append("")

    # 修正履歴
    for i, rev in enumerate(revisions, start=1):
        instruction = (rev.get("raw_instruction") or "").strip().replace("\n", " ")
        after = (rev.get("after_section") or "").rstrip()
        out.append(f"## 修正{i}: 「{instruction}」")
        out.append("")
        out.append(after)
        out.append("")

    # 決定版: parts を区切りで連結（実際に Threads に送信された本文の連投表現）
    out.append("## 決定版（実投稿本文）")
    out.append("")
    out.append("\n\n---\n\n".join(parts))
    out.append("")

    return "\n".join(out) + "\n"


def save_edit_case(
    *,
    state: dict,
    slot_name: str,
    candidate: dict,
    section: str,
    parts: list[str],
    profile: str,
    published_at: str,
    permalink: str | None,
) -> None:
    """publish 成功直後に呼ぶ。書き込み失敗は publish 全体を失敗させない。"""
    try:
        date_str = state.get("date") or ""
        candidate_id = candidate.get("id")
        if not date_str or candidate_id is None:
            log("WARN: edit case skip (missing date or candidate_id)")
            return
        EDITS_DIR.mkdir(parents=True, exist_ok=True)
        revisions = _load_revision_entries(date_str, candidate_id)
        content = _build_edit_case_md(
            state=state,
            slot_name=slot_name,
            candidate=candidate,
            section=section,
            parts=parts,
            profile=profile,
            published_at=published_at,
            permalink=permalink,
            revisions=revisions,
        )
        out_path = EDITS_DIR / f"{date_str}_{slot_name}_{candidate_id}.md"
        out_path.write_text(content, encoding="utf-8")
        log(f"edit case saved: {out_path.name} (revisions={len(revisions)})")
    except Exception as e:
        log(f"WARN: failed to save edit case: {e}")


# --- Discord 通知 ---
def enforce_emergency_stop_or_exit(slot_name: str, profile: str, date: str) -> None:
    """緊急停止センチネルを確認し、production プロファイルなら投稿せず終了する。

    詳細仕様: repo/01_active/緊急停止.md
    - --profile production のときだけセンチネルをチェック（test は素通り）
    - センチネル存在 → Discord 通知 + sys.exit(0)（launchd 上は成功扱い）
    - --dry-run でも guard は効く（動作確認のため）
    """
    if profile != "production":
        return
    if not EMERGENCY_STOP_SENTINEL.exists():
        return
    try:
        body = EMERGENCY_STOP_SENTINEL.read_text(encoding="utf-8").rstrip()
    except OSError:
        body = "(センチネル読み取り失敗)"
    log(f"EMERGENCY STOP active. skipping slot={slot_name} date={date} profile={profile}")
    notify_discord(
        f"⛔ 緊急停止中のため {date} {slot_name} 投稿をスキップしました\n"
        f"```\n{body}\n```\n"
        f"解除：/緊急停止解除"
    )
    sys.exit(0)


def notify_discord(message: str, file_path: Path | None = None) -> None:
    """notify_discord.sh 経由で #threads_auto に投稿。失敗しても投稿処理は止めない。"""
    notify_script = SCRIPT_DIR / "notify_discord.sh"
    if not notify_script.is_file():
        log(f"WARN: notify_discord.sh not found at {notify_script}, skip")
        return
    cmd = [str(notify_script), message]
    if file_path:
        cmd.append(str(file_path))
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
        if result.returncode == 0:
            log(f"discord notify OK")
        else:
            log(f"WARN: discord notify rc={result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
    except subprocess.TimeoutExpired:
        log("WARN: discord notify timeout (non-fatal)")
    except Exception as e:
        log(f"WARN: discord notify error (non-fatal): {e}")


# --- メイン処理 ---
SKIP_STATUSES = ("published", "failed", "skipped")
RESUME_STATUSES = ("partially_published", "publishing")
WAIT_BEFORE_PUBLISH_SEC = 5


def _post_one(token: str, user_id: str, text: str, reply_to_id: str | None, dry_run: bool) -> str:
    """create_container → 待機 → publish_container を 1 セットで実行し media_id を返す。"""
    if len(text) > 500:
        die(f"text length {len(text)} exceeds Threads 500-char limit (1投稿)")
    container_id = create_container(token, user_id, text, reply_to_id=reply_to_id, dry_run=dry_run)
    if not dry_run:
        log(f"waiting {WAIT_BEFORE_PUBLISH_SEC}s before publish (container 準備待ち)")
        time.sleep(WAIT_BEFORE_PUBLISH_SEC)
    return publish_container(token, user_id, container_id, dry_run=dry_run)


def publish_slot(state: dict, state_path: Path, slot_name: str, profile: str, dry_run: bool) -> None:
    token, user_id, username = get_profile_creds(profile)

    slot = next((s for s in state["slots"] if s["slot"] == slot_name), None)
    if not slot:
        die(f"slot '{slot_name}' not in state")

    # 冪等性
    if slot["status"] in SKIP_STATUSES:
        log(f"slot={slot_name} already status={slot['status']}, skipping (idempotent)")
        return

    # candidate
    candidate_id = slot.get("candidate_id")
    if not candidate_id:
        die(f"slot {slot_name} has no candidate_id (selected_ids 確定前？)")
    candidate = next((c for c in state["candidates"] if c["id"] == candidate_id), None)
    if not candidate:
        die(f"candidate id={candidate_id} not in state")
    if candidate["status"] != "approved":
        die(f"candidate {candidate_id} status={candidate['status']} (approved 必須)")

    # 承認ゲート: reservation candidate は事前承認扱いなので global approval が
    # 立っていなくても投稿可。それ以外は state.approval.approved を必須とする。
    is_reservation = candidate.get("type") == "reservation" or slot["status"] == "reserved"
    if not is_reservation and not state.get("approval", {}).get("approved"):
        die("approval.approved != true. 投稿しません（誤投稿防止）")

    # retry_count をインクリメント（このスロットへの publish.py 試行回数）
    if not dry_run:
        def _bump_retry(s: dict) -> None:
            sl = _find_slot(s, slot_name)
            sl["retry_count"] = (sl.get("retry_count") or 0) + 1
        state = update_state_atomically(state_path, _bump_retry)
        slot = _find_slot(state, slot_name)
        log(f"retry_count={slot['retry_count']}")

    # 本文抽出（連投分も全部）
    # candidate.source が set されていれば seeds/ 等の個別ファイルを優先。
    # post-drafter エージェント由来の seed candidate は state.post_file に乗らない。
    source_rel = candidate.get("source") or state["post_file"]
    post_file = ACTIVE_DIR / source_rel
    if not post_file.is_file():
        die(f"post file not found: {post_file}")
    section = extract_post_section(post_file, candidate_id)
    parts = extract_thread_parts(section)

    log(f"=== profile={profile} username={username} (user_id={user_id}) ===")
    log(f"=== slot={slot_name} candidate_id={candidate_id} type={candidate.get('type')} ===")
    log(f"=== thread parts: {len(parts)} ===")
    for i, part in enumerate(parts):
        first_line = part.split("\n", 1)[0][:60]
        log(f"  [{i+1}/{len(parts)}] len={len(part)} preview: {first_line}")

    # 再開判定：parent_id 既存ならそれを使い、reply_ids の続きから投稿
    existing_parent = slot.get("parent_id")
    existing_replies = slot.get("reply_ids") or []
    is_resume = bool(existing_parent) and slot["status"] in RESUME_STATUSES

    if is_resume:
        log(f"RESUMING: parent_id={existing_parent}, already posted replies={len(existing_replies)}")
        parent_id = existing_parent
        next_part_idx = 1 + len(existing_replies)
    else:
        # 親投稿
        if not dry_run:
            def _mark_publishing(s: dict) -> None:
                sl = _find_slot(s, slot_name)
                sl["status"] = "publishing"
                sl["profile"] = profile
            state = update_state_atomically(state_path, _mark_publishing)
            slot = _find_slot(state, slot_name)
        log(f"posting parent [1/{len(parts)}]")
        try:
            parent_id = _post_one(token, user_id, parts[0], reply_to_id=None, dry_run=dry_run)
        except ThreadsAPIError as e:
            err_msg = f"parent投稿失敗: {e}"
            err_str = str(e)
            if not dry_run:
                def _mark_failed(s: dict) -> None:
                    sl = _find_slot(s, slot_name)
                    sl["status"] = "failed"
                    sl["error"] = err_msg
                    s.setdefault("history", []).append({
                        "ts": _ts(), "event": "failed", "actor": "publish.py",
                        "slot": slot_name, "stage": "parent", "error": err_str,
                    })
                state = update_state_atomically(state_path, _mark_failed)
                slot = _find_slot(state, slot_name)
                notify_discord(
                    f"❌ {state['date']} {slot_name} ({profile}) 投稿失敗\n"
                    f"段階: 親投稿\n"
                    f"エラー: {e}\n"
                    f"手動投稿が必要です。"
                )
            else:
                slot["status"] = "failed"
                slot["error"] = err_msg
            raise
        if not dry_run:
            def _set_parent(s: dict) -> None:
                sl = _find_slot(s, slot_name)
                sl["parent_id"] = parent_id
            state = update_state_atomically(state_path, _set_parent)
            slot = _find_slot(state, slot_name)
        else:
            slot["parent_id"] = parent_id
        next_part_idx = 1

    # 連投（reply_to_id=root）
    for i in range(next_part_idx, len(parts)):
        log(f"posting reply [{i+1}/{len(parts)}]")
        try:
            reply_id = _post_one(token, user_id, parts[i], reply_to_id=parent_id, dry_run=dry_run)
        except ThreadsAPIError as e:
            err_str = str(e)
            failed_at = i + 1
            total = len(parts)
            if not dry_run:
                def _mark_partial(s: dict) -> None:
                    sl = _find_slot(s, slot_name)
                    sl["status"] = "partially_published"
                    completed_now = len(sl.get("reply_ids", []))
                    sl["error"] = f"reply投稿で失敗（part {failed_at}/{total}, {completed_now} reply完了）: {err_str}"
                    s.setdefault("history", []).append({
                        "ts": _ts(), "event": "partially_published", "actor": "publish.py",
                        "slot": slot_name, "parent_id": parent_id,
                        "completed_replies": completed_now, "total_parts": total,
                        "failed_at_part": failed_at, "error": err_str,
                    })
                state = update_state_atomically(state_path, _mark_partial)
                slot = _find_slot(state, slot_name)
                completed = len(slot.get("reply_ids", []))
                notify_discord(
                    f"⚠️ {state['date']} {slot_name} ({profile}) 部分投稿\n"
                    f"親: 成功（parent_id={parent_id}）\n"
                    f"reply: {completed}/{total-1} 完了で part{failed_at} 失敗\n"
                    f"エラー: {e}\n"
                    f"再実行で part{failed_at} から再開します。"
                )
            else:
                slot["status"] = "partially_published"
                completed = len(slot.get("reply_ids", []))
                slot["error"] = f"reply投稿で失敗（part {failed_at}/{total}, {completed} reply完了）: {err_str}"
            raise
        if not dry_run:
            def _append_reply(s: dict, _rid: str = reply_id) -> None:
                sl = _find_slot(s, slot_name)
                sl.setdefault("reply_ids", []).append(_rid)
            state = update_state_atomically(state_path, _append_reply)
            slot = _find_slot(state, slot_name)
        else:
            slot.setdefault("reply_ids", []).append(reply_id)

    # 全完了 → permalink 取得（root post）
    permalink = get_permalink(token, parent_id, dry_run=dry_run)
    published_ts = _ts()
    if not dry_run:
        def _mark_published(s: dict) -> None:
            sl = _find_slot(s, slot_name)
            sl["status"] = "published"
            sl["permalink"] = permalink
            sl["published_at"] = published_ts
            sl["error"] = None
            s.setdefault("history", []).append({
                "ts": published_ts,
                "event": "published",
                "actor": "publish.py",
                "slot": slot_name,
                "parent_id": parent_id,
                "reply_count": len(sl.get("reply_ids", [])),
                "profile": profile,
            })
        state = update_state_atomically(state_path, _mark_published)
        slot = _find_slot(state, slot_name)
        notify_discord(
            f"✅ {state['date']} {slot_name} ({profile}) 投稿成功\n"
            f"連投: {len(parts)} 本（親 + reply {len(parts)-1}）\n"
            f"{permalink or '(permalink取得失敗)'}"
        )
    else:
        slot["status"] = "published"
        slot["permalink"] = permalink
        slot["published_at"] = published_ts
        slot["error"] = None

    # 編集事例の保存（dry_run では書かない）
    if not dry_run:
        save_edit_case(
            state=state,
            slot_name=slot_name,
            candidate=candidate,
            section=section,
            parts=parts,
            profile=profile,
            published_at=published_ts,
            permalink=permalink,
        )

    log(f"=== DONE: slot={slot_name} parts={len(parts)} permalink={permalink} ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="Threads 自動投稿スクリプト（Phase 3 / 6a+6b+6c+6d）")
    parser.add_argument("--slot", required=True, choices=["morning", "noon", "evening"])
    parser.add_argument("--date", default=datetime.now(JST).strftime("%Y-%m-%d"))
    parser.add_argument("--profile", default="test", choices=["test", "production"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--randomize",
        action="store_true",
        help=f"投稿前に 0〜{RANDOMIZE_MAX_SEC} 秒のランダム sleep（launchd 用、dry-run 時は無視）",
    )
    args = parser.parse_args()

    # 緊急停止チェック（production のみ。test は素通り）
    # 詳細: repo/01_active/緊急停止.md
    enforce_emergency_stop_or_exit(args.slot, args.profile, args.date)

    load_env()
    state, state_path = load_state(args.date)
    log(f"=== publish.py start (date={args.date} slot={args.slot} profile={args.profile} dry_run={args.dry_run} randomize={args.randomize}) ===")

    # ±15 分ランダム化（launchd で投稿時刻を分散）
    if args.randomize and not args.dry_run:
        jitter = random.randint(0, RANDOMIZE_MAX_SEC)
        log(f"randomize: sleeping {jitter}s before publish")
        time.sleep(jitter)

    try:
        publish_slot(state, state_path, args.slot, args.profile, args.dry_run)
    except ThreadsAPIError:
        # publish_slot 内で notify_discord 済み・state 保存済み
        sys.exit(2)


if __name__ == "__main__":
    main()
