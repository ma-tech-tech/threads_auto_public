#!/usr/bin/env python3
"""Threads 投稿の 24h 反応値取得スクリプト（Phase 4-A）

state/*.json を走査し、対象 slot の Threads Insights API を叩いて
反応値（views/likes/replies/reposts/quotes）を取得・記録・通知する。

対象 slot の条件（AND）:
  - status == "published"
  - profile == --profile 引数
  - metrics_24h is None（未取得 or 過去取得失敗）
  - published_at が 24h 〜 7 日前の範囲

Usage:
  fetch_metrics.py --profile {test|production} [--date YYYY-MM-DD] [--dry-run]

設計判断:
  - profile は --profile 引数で指定（state.json slots[].profile は読まない / フィルタにのみ使用）
  - state 更新は update_state_atomically（fcntl ロック）経由
  - jsonl は O_APPEND で追記（OS の atomic write に依存）
  - views は API ドキュメントで「開発中」ラベル付きのため値の存在チェック必須
  - 全 metric の period は "lifetime"（24h 時点の累計値として扱う）
  - 取得失敗時は state を更新せず（null のまま次回再試行）
"""

import argparse
import fcntl
import json
import os
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

JST = timezone(timedelta(hours=9))
THREADS_API = "https://graph.threads.net/v1.0"
METRICS = ["views", "likes", "replies", "reposts", "quotes"]

# 取得対象とする published_at の経過時間範囲
MIN_AGE = timedelta(hours=24)
MAX_AGE = timedelta(days=7)

# API リトライ
API_MAX_ATTEMPTS = 3
API_INITIAL_BACKOFF_SEC = 1.0

# state ロック
LOCK_TIMEOUT_SEC = 30
LOCK_POLL_INTERVAL_SEC = 0.1


SLOT_LABEL_JA = {"morning": "朝", "noon": "昼", "evening": "夜"}
PROFILE_LABEL_SHORT = {"test": "test", "production": "prod"}


class ThreadsAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, response_text: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


# --- ロギング ---
def _ts() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{_ts()}] [fetch_metrics.py] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


# --- 環境変数（publish.py と同じパターン） ---
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


def get_token(profile: str) -> str:
    if profile == "production":
        token = os.environ.get("THREADS_ACCESS_TOKEN", "")
    elif profile == "test":
        token = os.environ.get("THREADS_TEST_ACCESS_TOKEN", "")
    else:
        die(f"unknown profile: {profile}")
    if not token:
        die(f"missing access token for profile={profile}")
    return token


# --- state ロック（publish.py と同じパターン） ---
@contextmanager
def state_lock(state_path: Path):
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
    with state_lock(state_path):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        mutator(state)
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return state


# --- 対象 slot 抽出 ---
def parse_published_at(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def collect_targets(profile: str, now: datetime, single_date: str | None = None) -> list[dict]:
    """state/*.json を走査して対象 slot を抽出。
    返り値の各要素: {state_path, date, slot_name, candidate_id, parent_id, published_at}"""
    state_dir = ACTIVE_DIR / "state"
    if single_date:
        files = [state_dir / f"{single_date}.json"]
    else:
        files = sorted(state_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"))

    targets: list[dict] = []
    for state_path in files:
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log(f"WARN: skip malformed state {state_path}: {e}")
            continue

        date = state.get("date") or state_path.stem
        for sl in state.get("slots", []):
            if sl.get("status") != "published":
                continue
            if sl.get("profile") != profile:
                continue
            # metrics_24h が無いキー or null は対象、辞書なら取得済みでスキップ
            if isinstance(sl.get("metrics_24h"), dict):
                continue
            published_at = parse_published_at(sl.get("published_at"))
            if not published_at:
                continue
            age = now - published_at
            if age < MIN_AGE or age > MAX_AGE:
                continue
            parent_id = sl.get("parent_id")
            if not parent_id:
                continue
            targets.append({
                "state_path": state_path,
                "date": date,
                "slot_name": sl["slot"],
                "candidate_id": sl.get("candidate_id"),
                "parent_id": parent_id,
                "published_at": sl.get("published_at"),
                "permalink": sl.get("permalink"),
            })
    return targets


def find_candidate_type(state: dict, candidate_id: int | None) -> str | None:
    if candidate_id is None:
        return None
    for c in state.get("candidates", []):
        if c.get("id") == candidate_id:
            return c.get("type")
    return None


# --- Threads Insights API ---
def _fetch_insights_once(token: str, media_id: str) -> dict[str, int | None]:
    url = f"{THREADS_API}/{media_id}/insights"
    params = {"metric": ",".join(METRICS), "access_token": token}
    try:
        r = requests.get(url, params=params, timeout=30)
    except requests.RequestException as e:
        raise ThreadsAPIError(f"network error: {e}") from e
    if r.status_code >= 400:
        raise ThreadsAPIError(
            f"insights HTTP {r.status_code}",
            status_code=r.status_code,
            response_text=r.text,
        )
    body = r.json()
    out: dict[str, int | None] = {m: None for m in METRICS}
    for entry in body.get("data", []):
        name = entry.get("name")
        values = entry.get("values") or []
        if name in out:
            if values and isinstance(values[0], dict) and "value" in values[0]:
                out[name] = values[0]["value"]
            else:
                # 「開発中」指標などで value が空のケース
                out[name] = None
    return out


def fetch_insights(token: str, media_id: str) -> dict[str, int | None]:
    last_err: ThreadsAPIError | None = None
    for attempt in range(1, API_MAX_ATTEMPTS + 1):
        try:
            return _fetch_insights_once(token, media_id)
        except ThreadsAPIError as e:
            last_err = e
            log(f"  insights attempt {attempt}/{API_MAX_ATTEMPTS} failed: {e} (status={e.status_code})")
            if attempt < API_MAX_ATTEMPTS:
                backoff = API_INITIAL_BACKOFF_SEC * (2 ** (attempt - 1))
                time.sleep(backoff)
    assert last_err is not None
    raise last_err


# --- 永続化 ---
def write_metrics_to_state(state_path: Path, slot_name: str, metrics: dict[str, int | None]) -> None:
    fetched_at = _ts()

    def _mutator(s: dict) -> None:
        for sl in s.get("slots", []):
            if sl.get("slot") == slot_name:
                sl["metrics_24h"] = {
                    "fetched_at": fetched_at,
                    **{m: metrics.get(m) for m in METRICS},
                }
                break
        s.setdefault("history", []).append({
            "ts": fetched_at,
            "event": "metrics_fetched",
            "actor": "fetch_metrics.py",
            "slot": slot_name,
        })

    update_state_atomically(state_path, _mutator)


def append_jsonl(date: str, slot_name: str, profile: str, candidate_id: int | None,
                  candidate_type: str | None, parent_id: str, metrics: dict[str, int | None]) -> None:
    yyyymm = date[:7]
    jsonl_path = ACTIVE_DIR / "logs" / "metrics" / f"{yyyymm}.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "date": date,
        "slot": slot_name,
        "profile": profile,
        "candidate_id": candidate_id,
        "type": candidate_type,
        "parent_id": parent_id,
        "fetched_at": _ts(),
        **{m: metrics.get(m) for m in METRICS},
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    # O_APPEND で atomic 追記（複数プロセスからの追記でも 1 行が混ざらない）
    fd = os.open(str(jsonl_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


# --- Discord 通知 ---
def notify_discord(message: str) -> None:
    notify_script = SCRIPT_DIR / "notify_discord.sh"
    if not notify_script.is_file():
        log(f"WARN: notify_discord.sh not found, skip")
        return
    try:
        result = subprocess.run([str(notify_script), message], capture_output=True, timeout=30, text=True)
        if result.returncode == 0:
            log(f"discord notify OK")
        else:
            log(f"WARN: discord notify rc={result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
    except subprocess.TimeoutExpired:
        log("WARN: discord notify timeout (non-fatal)")
    except Exception as e:
        log(f"WARN: discord notify error (non-fatal): {e}")


def fmt_metric_line(slot_name: str, metrics: dict[str, int | None], permalink: str | None) -> str:
    label = SLOT_LABEL_JA.get(slot_name, slot_name)
    parts: list[str] = []
    likes = metrics.get("likes")
    views = metrics.get("views")
    replies = metrics.get("replies")
    reposts = metrics.get("reposts")
    quotes = metrics.get("quotes")

    parts.append(f"{likes if likes is not None else '?'} likes")
    parts.append(f"{views if views is not None else '?'} views")
    if replies is not None:
        parts.append(f"{replies} {'reply' if replies == 1 else 'replies'}")
    if reposts:
        parts.append(f"{reposts} reposts")
    if quotes:
        parts.append(f"{quotes} {'quote' if quotes == 1 else 'quotes'}")
    line = f"{label}: " + " / ".join(parts)
    if permalink:
        line += f"  {permalink}"
    return line


def fmt_failure_line(slot_name: str, reason: str) -> str:
    label = SLOT_LABEL_JA.get(slot_name, slot_name)
    return f"{label}: ⚠ 取得失敗（{reason}）"


# --- メイン ---
def main() -> None:
    parser = argparse.ArgumentParser(description="Threads 24h 反応値取得（Phase 4-A）")
    parser.add_argument("--profile", required=True, choices=["test", "production"])
    parser.add_argument(
        "--date",
        help="特定日の state ファイルだけを対象にする（省略時は state/ 全体を走査）",
    )
    parser.add_argument("--dry-run", action="store_true", help="API は叩くが state/jsonl/Discord は更新しない")
    args = parser.parse_args()

    load_env()
    token = get_token(args.profile)
    now = datetime.now(JST)
    log(f"=== fetch_metrics.py start (profile={args.profile} date={args.date or 'ALL'} dry_run={args.dry_run} now={now.isoformat()}) ===")

    targets = collect_targets(args.profile, now, single_date=args.date)
    if not targets:
        log(f"対象 slot なし（profile={args.profile}）")
        return

    log(f"対象 slot: {len(targets)} 件")
    for t in targets:
        log(f"  - {t['date']} {t['slot_name']} parent_id={t['parent_id']} (published_at={t['published_at']})")

    # 日付ごとに集約して 1 メッセージで通知（複数日分を一気に取った場合でも日付別）
    results_by_date: dict[str, list[tuple[dict, dict[str, int | None] | None, str | None]]] = {}

    for t in targets:
        log(f"fetch: {t['date']} {t['slot_name']} parent_id={t['parent_id']}")
        try:
            metrics = fetch_insights(token, t["parent_id"])
            log(f"  -> {metrics}")
            if not args.dry_run:
                write_metrics_to_state(t["state_path"], t["slot_name"], metrics)
                # state から candidate_type を引き直す
                state = json.loads(t["state_path"].read_text(encoding="utf-8"))
                ctype = find_candidate_type(state, t["candidate_id"])
                append_jsonl(
                    date=t["date"],
                    slot_name=t["slot_name"],
                    profile=args.profile,
                    candidate_id=t["candidate_id"],
                    candidate_type=ctype,
                    parent_id=t["parent_id"],
                    metrics=metrics,
                )
            results_by_date.setdefault(t["date"], []).append((t, metrics, None))
        except ThreadsAPIError as e:
            log(f"  ERROR: {e}")
            results_by_date.setdefault(t["date"], []).append((t, None, str(e)))

    if args.dry_run:
        log("dry-run のため Discord 通知はスキップ")
        return

    profile_label = PROFILE_LABEL_SHORT.get(args.profile, args.profile)

    for date, items in sorted(results_by_date.items()):
        # slot 順（朝→昼→夜）でソート
        slot_order = {"morning": 0, "noon": 1, "evening": 2}
        items.sort(key=lambda x: slot_order.get(x[0]["slot_name"], 99))

        all_failed = all(err is not None for _, _, err in items)
        if all_failed:
            msg = f"⚠️ [{profile_label}] {date} 反応値の取得に失敗（次回再試行）"
            for t, _, err in items:
                msg += "\n" + fmt_failure_line(t["slot_name"], err or "unknown")
            notify_discord(msg)
            continue

        msg = f"📊 [{profile_label}] {date} 反応値（24h）"
        for t, metrics, err in items:
            if err is not None:
                msg += "\n" + fmt_failure_line(t["slot_name"], err)
            else:
                msg += "\n" + fmt_metric_line(t["slot_name"], metrics or {}, t.get("permalink"))
        notify_discord(msg)

    log("=== done ===")


if __name__ == "__main__":
    main()
