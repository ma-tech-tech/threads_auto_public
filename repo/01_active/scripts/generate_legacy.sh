#!/bin/bash
# 5:00 cron (launchd) で起動。生成プロンプトを claude --print に渡し、
# posts/YYYY-MM-DD_5案.md に出力する。
#
# 流れ: claude --print → posts/ 書き出し → url_check（URL 死活チェック後処理）
#       → init_state.py → notify_discord.sh
# url_check は本文中の URL を curl HEAD で検査し、dead を URL 行＋決まり文句行
# ごと削除する（プロンプトには書かず、ここで機械処理）。
#
# 使い方（手動実行）:
#   bash 01_active/scripts/generate.sh
#
# 自動実行は 01_active/launchd/com.example.threads_auto.generate.plist で登録。

set -euo pipefail

# launchd 起動時は PATH が空なので明示する
export PATH="__USER_LOCAL_BIN__:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# スクリプト自身の絶対パスを cd 前に確定する（$0 の相対起動でも壊れないように）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# スクリプトの2階層上（01_active/）を作業ディレクトリに固定
cd "${SCRIPT_DIR}/.."

# 実行日（ログファイル用）と投稿対象日（state/posts ファイル用）を分ける。
# 5:00 起動 → 翌日付の 5 案を作るスキーマ（2026-05-06 移行）。
# 13 時頃 OWNER が確認 → 翌朝・翌昼・翌夜の 3 投稿として publish される。
TODAY=$(date +%Y-%m-%d)                  # 実行日（log ファイル名のみ）
TARGET_DATE=$(date -v+1d +%Y-%m-%d)      # 投稿対象日 = 明日。posts/state/reservations/ファイル名はこちら
TS=$(date +%Y-%m-%d_%H:%M:%S)
PROMPT_FILE="bk/03_生成プロンプト_v1_premerge.md"
OUTPUT_FILE="posts/${TARGET_DATE}_5案.md"
LOG_FILE="logs/${TODAY}.log"

mkdir -p posts logs

log() {
  echo "[${TS}] $*" >> "${LOG_FILE}"
}

# URL 死活チェック
# 引数: $1 = 対象ファイル
# 動作:
#   - 「## 候補ネタリスト」より前の本文中の URL を抽出
#   - 各 URL に curl -I -L --max-time 5 で HEAD リクエスト
#   - HTTP 200/301/302 以外（タイムアウト含む）は NG とし、
#     その URL を含む行と、直前が決まり文句なら直前行も削除
#   - 削除結果を atomic に書き戻す
url_check() {
  local target_file="$1"
  if [ ! -f "${target_file}" ]; then
    log "url_check: target not found: ${target_file}"
    return 0
  fi

  # 検証範囲：先頭〜「## 候補ネタリスト」の直前
  local body_lines
  body_lines=$(awk '
    /^## 候補ネタリスト/ { exit }
    { print NR }
  ' "${target_file}")

  # 検証範囲内の URL を抽出（重複除去）
  local urls
  urls=$(awk '
    /^## 候補ネタリスト/ { exit }
    {
      s = $0
      while (match(s, /https?:\/\/[^[:space:])\]"\047]+/)) {
        u = substr(s, RSTART, RLENGTH)
        # 末尾の句読点を落とす
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
    # curl -w "%{http_code}" は失敗時も "000" を出力する。
    # ただし set -e 下で curl 非0終了が伝播しないよう || true で吸収する。
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

  # NG URL を含む行と、直前の決まり文句行を削除
  # body_end_line = 検証範囲の最終行番号
  local body_end_line
  body_end_line=$(echo "${body_lines}" | tail -n 1)
  [ -z "${body_end_line}" ] && body_end_line=0

  # NG URL リストを一時ファイルに書く（BSD awk は -v に改行を含められないため）
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
      # body_end を超えた範囲はノータッチ
      if (NR > body_end) {
        if (have_prev) { print prev; have_prev = 0 }
        print
        next
      }
      if (line_has_dead_url($0)) {
        # この行を削除。直前が決まり文句なら直前も削除
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

if [ ! -f "${PROMPT_FILE}" ]; then
  log "ERROR: プロンプトファイルが見つからない: ${PROMPT_FILE}"
  exit 1
fi

# 予約済みスロット（reservations/<TARGET_DATE>_<slot>.md）が存在するか事前チェック。
# あれば Discord に予告通知 + ログに記録。生成自体は止めない（残スロット用の 5 案
# 候補として使えるため）。init_state.py が apply_reservations を呼んで slot を
# status=reserved に固定するので、その後の `--select 1,3,5` は state_update.py 側で
# 拒否される（事故防止）。
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

# プロンプト本体（"## プロンプト本体（ここから下をコピペ）"〜"## プロンプト本体（ここまで）"）を抽出し、
# 日付プレースホルダ YYYY-MM-DD を投稿対象日（明日）に置換
PROMPT_BODY=$(awk '
  /^## プロンプト本体（ここから下をコピペ）/ { capture=1; next }
  /^## プロンプト本体（ここまで）/         { capture=0; next }
  capture                                   { print }
' "${PROMPT_FILE}" | sed "s/YYYY-MM-DD/${TARGET_DATE}/g")

if [ -z "${PROMPT_BODY}" ]; then
  log "ERROR: プロンプト本体の抽出に失敗（マーカー行を確認）"
  exit 1
fi

log "generate.sh start (run_date=${TODAY}, target_date=${TARGET_DATE}, prompt=${#PROMPT_BODY}chars)"

# claude --print 実行
# - --output-format text: 標準出力にテキストを流す
# - --permission-mode acceptEdits: ツール使用を自動承認（対話的な許可待ちを回避）
# - --allowedTools: 5案生成に必要な読み取り系のみ許可（投稿はPhase 3でpublish.pyが担当）
# - --no-session-persistence: cron運用なのでセッションを保存しない
# - --max-budget-usd 5: 1回の生成で$5を超えたら停止（暴走防止）
# 結果は一旦 .tmp に書き出し、成功時のみ rename で確定
if claude \
    --print \
    --output-format text \
    --permission-mode acceptEdits \
    --allowedTools "Read WebSearch WebFetch Glob Grep" \
    --no-session-persistence \
    --max-budget-usd 5 \
    > "${OUTPUT_FILE}.tmp" 2>>"${LOG_FILE}" <<EOF
${PROMPT_BODY}
EOF
then
  mv "${OUTPUT_FILE}.tmp" "${OUTPUT_FILE}"
  BYTES=$(wc -c < "${OUTPUT_FILE}")
  log "generate.sh done -> ${OUTPUT_FILE} (${BYTES} bytes)"

  # URL 死活チェック（本文中の URL のみ。候補ネタリスト以降は対象外）
  # 失敗しても全体は止めない（curl 失敗 = 当該 URL を NG 扱いにするだけ）
  url_check "${OUTPUT_FILE}" || log "WARN: url_check raised an error (continuing)"

  # state.json 初期化（candidates[] を posts/ から自動抽出）
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

  # Discord に 5 案ファイルを Bot 投稿（失敗してもファイル生成は成功扱い）
  NOTIFY_SCRIPT="${SCRIPT_DIR}/notify_discord.sh"
  if [ -x "${NOTIFY_SCRIPT}" ]; then
    NOTIFY_MSG="🌅 ${TARGET_DATE} の 5 案できました（明日の朝・昼・夜の 3 投稿用）
お昼ごろ目を通して、修正したい箇所はこのスレッド内でリプライしてください
例：「2の冒頭短く」「3はやり直し」「1,3,5で確定」"
    if "${NOTIFY_SCRIPT}" "${NOTIFY_MSG}" "${OUTPUT_FILE}" >> "${LOG_FILE}" 2>&1; then
      log "discord notify OK"
    else
      log "WARN: discord notify FAILED (rc=$?), file is still saved at ${OUTPUT_FILE}"
    fi
  else
    log "WARN: ${NOTIFY_SCRIPT} not executable, skipping discord notify"
  fi

  exit 0
else
  RC=$?
  rm -f "${OUTPUT_FILE}.tmp"
  log "generate.sh FAILED (rc=${RC})"
  exit "${RC}"
fi
