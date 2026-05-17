#!/bin/bash
# 5:00 cron (launchd) で起動。news-curator → post-writer の 2 段階で投稿対象日の 5 案を生成する。
#
# 流れ:
#   Step 1: .claude/agents/news-curator.md → claude --print → logs/news/<date>_news.md
#   Step 2: .claude/agents/post-writer.md + 中間ファイル → claude --print → posts/<date>_5案.md
#   後処理: url_check → init_state.py → notify_discord.sh
#
# 使い方（手動実行）:
#   bash 01_active/scripts/generate.sh
#
# 並走テスト時：
#   GENERATE_OUTPUT_DIR=test bash 01_active/scripts/generate.sh
#   → logs/news/test/, posts/test/ に出力（init_state / notify は走らない）
#
# E2E 通知確認時（test ファイル + Discord 通知のみ実行）：
#   GENERATE_OUTPUT_DIR=test NOTIFY_IN_TEST=1 bash 01_active/scripts/generate.sh
#   → 上記に加えて test ファイルを添付して #threads_auto に [test] プレフィックス通知
#
# 自動実行は 01_active/launchd/com.example.threads_auto.generate.plist で登録。

set -euo pipefail

# launchd 起動時は PATH が空なので明示する
export PATH="__USER_LOCAL_BIN__:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# スクリプト自身の絶対パスを cd 前に確定する
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 01_active/ を作業ディレクトリに固定
cd "${SCRIPT_DIR}/.."

# プロジェクトルート（.claude/agents/ を持つ場所）
# SCRIPT_DIR = repo/01_active/scripts → 3 つ上が project root
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

# 実行日（ログファイル用）と投稿対象日（state/posts ファイル用）を分ける。
# 5:00 起動 → 翌日付の 5 案を作るスキーマ（2026-05-06 移行）。
TODAY=$(date +%Y-%m-%d)                  # 実行日（log ファイル名のみ）
TARGET_DATE=$(date -v+1d +%Y-%m-%d)      # 投稿対象日 = 明日。posts/state/reservations/ファイル名はこちら
TS=$(date +%Y-%m-%d_%H:%M:%S)

# 出力ディレクトリの切替（並走テスト用）
# GENERATE_OUTPUT_DIR=test を指定すると test サブディレクトリに出して init_state / notify を skip
OUTPUT_PREFIX="${GENERATE_OUTPUT_DIR:-}"
if [ -n "${OUTPUT_PREFIX}" ]; then
  NEWS_DIR="logs/news/${OUTPUT_PREFIX}"
  POSTS_DIR="posts/${OUTPUT_PREFIX}"
  IS_TEST=1
else
  NEWS_DIR="logs/news"
  POSTS_DIR="posts"
  IS_TEST=0
fi

# Agent ファイル
AGENT_NEWS="${PROJECT_ROOT}/.claude/agents/news-curator.md"
AGENT_POST="${PROJECT_ROOT}/.claude/agents/post-writer.md"

# 出力ファイル
NEWS_FILE="${NEWS_DIR}/${TARGET_DATE}_news.md"
OUTPUT_FILE="${POSTS_DIR}/${TARGET_DATE}_5案.md"
LOG_FILE="logs/${TODAY}.log"

mkdir -p "${POSTS_DIR}" "${NEWS_DIR}" logs

log() {
  echo "[${TS}] $*" >> "${LOG_FILE}"
}

# Agent 本体（frontmatter を除いた本文）を抽出
# YAML frontmatter は --- で始まり --- で終わる。それ以降が本文
extract_body() {
  awk 'BEGIN{c=0} /^---$/{c++; next} c>=2' "$1"
}

# Agent frontmatter から tools 行を抽出して --allowedTools 用にスペース区切りに変換
# 例：「Read, WebSearch, WebFetch, Glob, Grep」 → 「Read WebSearch WebFetch Glob Grep」
extract_tools() {
  awk -F': *' '/^tools: /{print $2; exit}' "$1" | sed 's/, */ /g'
}

# URL 死活チェック（旧 generate.sh から踏襲）
# 引数: $1 = 対象ファイル
url_check() {
  local target_file="$1"
  if [ ! -f "${target_file}" ]; then
    log "url_check: target not found: ${target_file}"
    return 0
  fi

  local body_lines
  body_lines=$(awk '
    /^## 候補ネタリスト/ { exit }
    { print NR }
  ' "${target_file}")

  local urls
  urls=$(awk '
    /^## 候補ネタリスト/ { exit }
    {
      s = $0
      while (match(s, /https?:\/\/[^[:space:])\]"\047]+/)) {
        u = substr(s, RSTART, RLENGTH)
        sub(/[、。,.;:]+$/, "", u)
        print u
        s = substr(s, RSTART + RLENGTH)
      }
    }
  ' "${target_file}" | awk '!seen[$0]++')

  if [ -z "${urls}" ]; then
    log "url_check: scanned=0 alive=0 dead=0 (no URLs in body)"
    return 0
  fi

  local scanned=0 alive=0 dead=0
  local dead_urls=""
  while IFS= read -r url; do
    [ -z "${url}" ] && continue
    scanned=$((scanned + 1))
    local code
    code=$(curl -I -L --max-time 5 -o /dev/null -s -w "%{http_code}" "${url}" 2>/dev/null || true)
    [ -z "${code}" ] && code="000"
    case "${code}" in
      200|301|302)
        alive=$((alive + 1))
        ;;
      *)
        dead=$((dead + 1))
        local reason="HTTP ${code}"
        if [ "${code}" = "000" ]; then
          reason="timeout/connect-error"
        fi
        log "url_check: dead url removed: ${url} (${reason})"
        dead_urls="${dead_urls}${url}"$'\n'
        ;;
    esac
  done <<< "${urls}"

  if [ "${dead}" -eq 0 ]; then
    log "url_check: scanned=${scanned} alive=${alive} dead=0"
    return 0
  fi

  local body_end_line
  body_end_line=$(echo "${body_lines}" | tail -n 1)
  [ -z "${body_end_line}" ] && body_end_line=0

  local dead_list_file="${target_file}.deadurls.tmp"
  printf '%s' "${dead_urls}" > "${dead_list_file}"

  local tmp_file="${target_file}.urlcheck.tmp"
  awk -v body_end="${body_end_line}" -v dead_list="${dead_list_file}" '
    BEGIN {
      while ((getline line < dead_list) > 0) {
        if (line != "") dead[line] = 1
      }
      close(dead_list)
      prev = ""
      have_prev = 0
    }
    function is_boilerplate(line) {
      if (line ~ /公式の発表（英語）はこちらです/) return 1
      if (line ~ /公式の発表はこちらです/)         return 1
      if (line ~ /参考にしたデータはこちらです/)   return 1
      if (line ~ /参考にした事例はこちらです/)     return 1
      if (line ~ /元の記事はこちらです/)           return 1
      return 0
    }
    function line_has_dead_url(line,    s, u) {
      s = line
      while (match(s, /https?:\/\/[^[:space:])\]"\047]+/)) {
        u = substr(s, RSTART, RLENGTH)
        sub(/[、。,.;:]+$/, "", u)
        if (u in dead) return 1
        s = substr(s, RSTART + RLENGTH)
      }
      return 0
    }
    {
      if (NR > body_end) {
        if (have_prev) { print prev; have_prev = 0 }
        print
        next
      }
      if (line_has_dead_url($0)) {
        if (have_prev) {
          if (is_boilerplate(prev)) {
            have_prev = 0
          } else {
            print prev
            have_prev = 0
          }
        }
        next
      }
      if (have_prev) print prev
      prev = $0
      have_prev = 1
    }
    END {
      if (have_prev) print prev
    }
  ' "${target_file}" > "${tmp_file}" && mv "${tmp_file}" "${target_file}"

  rm -f "${dead_list_file}"

  log "url_check: scanned=${scanned} alive=${alive} dead=${dead}"
}

# === Step 1: ネタ収集 ===
# news-curator agent 本体を抽出して claude --print に渡す。出力は ${NEWS_FILE}
run_step1() {
  if [ ! -f "${AGENT_NEWS}" ]; then
    log "ERROR: news-curator agent not found: ${AGENT_NEWS}"
    return 1
  fi

  local body
  body=$(extract_body "${AGENT_NEWS}" | sed "s/YYYY-MM-DD/${TARGET_DATE}/g")
  if [ -z "${body}" ]; then
    log "ERROR: news-curator body extract failed"
    return 1
  fi

  local tools
  tools=$(extract_tools "${AGENT_NEWS}")
  if [ -z "${tools}" ]; then
    tools="Read WebSearch WebFetch Glob Grep"  # fallback
    log "WARN: news-curator tools not found in frontmatter, using fallback: ${tools}"
  fi

  local t0=$(date +%s)
  if claude \
        --print \
        --output-format text \
        --permission-mode acceptEdits \
        --allowedTools ${tools} \
        --no-session-persistence \
        --max-budget-usd 3 \
        > "${NEWS_FILE}.tmp" 2>>"${LOG_FILE}" <<EOF
${body}
EOF
  then
    # 最小バリデーション：候補ネタリストの表本体行 ≥ 8
    local rows
    rows=$(awk '/^## 候補ネタリスト/{f=1;next} /^## /{f=0} f && /^\| *[0-9]+ *\|/' "${NEWS_FILE}.tmp" | wc -l | tr -d ' ')
    if [ "${rows}" -lt 8 ]; then
      log "WARN: step1 candidate rows=${rows} (<8)"
      rm -f "${NEWS_FILE}.tmp"
      return 1
    fi
    mv "${NEWS_FILE}.tmp" "${NEWS_FILE}"
    local elapsed=$(($(date +%s) - t0))
    log "step1 done -> ${NEWS_FILE} (rows=${rows}, ${elapsed}s)"
    return 0
  fi
  rm -f "${NEWS_FILE}.tmp"
  return 1
}

# === Step 2: 記事作成 ===
# post-writer agent 本体に中間ファイルパスを末尾 append して claude --print に渡す。出力は ${OUTPUT_FILE}
run_step2() {
  if [ ! -f "${AGENT_POST}" ]; then
    log "ERROR: post-writer agent not found: ${AGENT_POST}"
    return 1
  fi
  if [ ! -f "${NEWS_FILE}" ]; then
    log "ERROR: news file not found: ${NEWS_FILE}"
    return 1
  fi

  local body
  body=$(extract_body "${AGENT_POST}" | sed "s/YYYY-MM-DD/${TARGET_DATE}/g")
  if [ -z "${body}" ]; then
    log "ERROR: post-writer body extract failed"
    return 1
  fi

  local tools
  tools=$(extract_tools "${AGENT_POST}")
  if [ -z "${tools}" ]; then
    tools="Read"  # fallback
    log "WARN: post-writer tools not found in frontmatter, using fallback: ${tools}"
  fi

  # 中間ファイル絶対パスを末尾に append
  local news_abs_path="$(pwd)/${NEWS_FILE}"
  body="${body}

【入力ファイル絶対パス】 ${news_abs_path}
"

  local t0=$(date +%s)
  if claude \
        --print \
        --output-format text \
        --permission-mode acceptEdits \
        --allowedTools ${tools} \
        --no-session-persistence \
        --max-budget-usd 2 \
        > "${OUTPUT_FILE}.tmp" 2>>"${LOG_FILE}" <<EOF
${body}
EOF
  then
    mv "${OUTPUT_FILE}.tmp" "${OUTPUT_FILE}"
    local elapsed=$(($(date +%s) - t0))
    local bytes=$(wc -c < "${OUTPUT_FILE}" | tr -d ' ')
    log "step2 done -> ${OUTPUT_FILE} (${bytes} bytes, ${elapsed}s)"
    return 0
  fi
  rm -f "${OUTPUT_FILE}.tmp"
  return 1
}

# ---------- main ----------

log "generate.sh start (run_date=${TODAY}, target_date=${TARGET_DATE}, test_mode=${IS_TEST})"

# 予約済みスロットの事前チェック（本番実行時のみ）
if [ "${IS_TEST}" -eq 0 ]; then
  RESERVED_SLOTS=()
  for SLOT in morning noon evening; do
    if [ -f "reservations/${TARGET_DATE}_${SLOT}.md" ]; then
      RESERVED_SLOTS+=("${SLOT}")
    fi
  done
  if [ "${#RESERVED_SLOTS[@]}" -gt 0 ]; then
    RESERVED_LIST=$(IFS=', '; echo "${RESERVED_SLOTS[*]}")
    log "reservations detected for ${TARGET_DATE}: ${RESERVED_LIST}"
    NOTIFY_SCRIPT="${SCRIPT_DIR}/notify_discord.sh"
    if [ -x "${NOTIFY_SCRIPT}" ]; then
      PRENOTIFY_MSG="🗓️ ${TARGET_DATE} は事前予約済みスロットがあります：${RESERVED_LIST}
5 案は通常通り生成しますが、予約済みスロットは予約内容を優先します。
予約を解除したい場合は \`reservations/${TARGET_DATE}_<slot>.md\` を削除してください。"
      "${NOTIFY_SCRIPT}" "${PRENOTIFY_MSG}" >> "${LOG_FILE}" 2>&1 \
        && log "discord pre-notify (reservations) OK" \
        || log "WARN: discord pre-notify FAILED (rc=$?)"
    fi
  fi
fi

# Step 1（1 回再試行）
if ! run_step1; then
  log "step1 retry"
  if ! run_step1; then
    log "ERROR: step1 failed twice, abort"
    exit 1
  fi
fi

# === Phase 4 hook (no-op in Phase 1) ===
# preferences_filter "${NEWS_FILE}" || log "WARN: preferences filter failed (continue)"

# Step 2
if ! run_step2; then
  log "ERROR: step2 failed"
  exit 1
fi

# 後処理（test モードでは url_check / init_state は skip）
# ただし NOTIFY_IN_TEST=1 を指定すると notify_discord は test ファイルで実行する
# （E2E 動作確認・通知の見た目チェック用）
NOTIFY_IN_TEST="${NOTIFY_IN_TEST:-0}"
if [ "${IS_TEST}" -eq 1 ]; then
  if [ "${NOTIFY_IN_TEST}" -eq 1 ]; then
    log "test mode: skip url_check / init_state, but notify_discord is enabled (NOTIFY_IN_TEST=1)"
    NOTIFY_SCRIPT="${SCRIPT_DIR}/notify_discord.sh"
    if [ -x "${NOTIFY_SCRIPT}" ]; then
      NOTIFY_MSG="🧪 [test] ${TARGET_DATE} の 5 案できました（test ファイル / 本番反映なし）
無視 OK。通知の見た目確認用です。
📎 添付：5 案本体 + ネタ収集ログ"
      if "${NOTIFY_SCRIPT}" "${NOTIFY_MSG}" "${OUTPUT_FILE}" "${NEWS_FILE}" >> "${LOG_FILE}" 2>&1; then
        log "discord notify OK (test)"
      else
        log "WARN: discord notify FAILED in test (rc=$?)"
      fi
    fi
  else
    log "test mode: skip url_check / init_state / notify_discord"
  fi
  log "generate.sh done (test) -> ${OUTPUT_FILE}"
  exit 0
fi

# URL 死活チェック
url_check "${OUTPUT_FILE}" || log "WARN: url_check raised an error (continuing)"

# state.json 初期化
INIT_SCRIPT="${SCRIPT_DIR}/init_state.py"
VENV_PY="__REPO_ROOT__/.venv/bin/python3"
if [ -x "${INIT_SCRIPT}" ] && [ -x "${VENV_PY}" ]; then
  if "${VENV_PY}" "${INIT_SCRIPT}" --date "${TARGET_DATE}" >> "${LOG_FILE}" 2>&1; then
    log "init_state OK -> state/${TARGET_DATE}.json"
  else
    log "WARN: init_state FAILED (rc=$?), publish.py は state file not found で止まります"
  fi
else
  log "WARN: init_state.py or venv python not found, skipping state init"
fi

# Discord 通知
NOTIFY_SCRIPT="${SCRIPT_DIR}/notify_discord.sh"
if [ -x "${NOTIFY_SCRIPT}" ]; then
  NOTIFY_MSG="🌅 ${TARGET_DATE} の 5 案できました（明日の朝・昼・夜の 3 投稿用）
お昼ごろ目を通して、修正したい箇所はこのスレッド内でリプライしてください
例：「2の冒頭短く」「3はやり直し」「1,3,5で確定」
📎 添付：5 案本体 + ネタ収集ログ（news-curator の出力、参考用）"
  if "${NOTIFY_SCRIPT}" "${NOTIFY_MSG}" "${OUTPUT_FILE}" "${NEWS_FILE}" >> "${LOG_FILE}" 2>&1; then
    log "discord notify OK"
  else
    log "WARN: discord notify FAILED (rc=$?), file is still saved at ${OUTPUT_FILE}"
  fi
else
  log "WARN: ${NOTIFY_SCRIPT} not executable, skipping discord notify"
fi

log "generate.sh done -> ${OUTPUT_FILE}"
exit 0
