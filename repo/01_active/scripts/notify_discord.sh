#!/bin/bash
# Discord Bot として #threads_auto に投稿する小さなヘルパー。
# generate.sh / publish.py 共通で使う想定。
#
# 使い方:
#   notify_discord.sh "<本文>"                                   # テキストのみ
#   notify_discord.sh "<本文>" /path/to/file.md                  # 1 ファイル添付
#   notify_discord.sh "<本文>" /path/to/file1.md /path/to/file2.md  # 複数ファイル添付（最大 10）
#
# 必要な環境変数:
#   DISCORD_BOT_TOKEN  ... ~/.claude/channels/discord/.env から自動ロード
#   DISCORD_CHANNEL_ID ... プロジェクトルート .env から自動ロード

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE_BOT="${HOME}/.claude/channels/discord/.env"
# scripts -> 01_active -> repo -> threads_auto
ENV_FILE_PROJ="${SCRIPT_DIR}/../../../.env"

[ -f "${ENV_FILE_BOT}" ] && { set -a; . "${ENV_FILE_BOT}"; set +a; }
[ -f "${ENV_FILE_PROJ}" ] && { set -a; . "${ENV_FILE_PROJ}"; set +a; }

: "${DISCORD_BOT_TOKEN:?DISCORD_BOT_TOKEN not set}"
: "${DISCORD_CHANNEL_ID:?DISCORD_CHANNEL_ID not set}"

CONTENT="${1:?content required}"
shift
FILES=("$@")  # 残り引数を全部ファイルとして扱う（0〜10 個）

URL="https://discord.com/api/v10/channels/${DISCORD_CHANNEL_ID}/messages"
PAYLOAD=$(python3 -c 'import sys,json; print(json.dumps({"content": sys.argv[1]}))' "${CONTENT}")

# 実在するファイルだけ -F files[i]=@... を組み立てる
# macOS bash 3.2 + set -u では空配列の "${FILES[@]}" が unbound variable 扱いになるので、
# 要素 0 のときは for ループ自体を skip する。
FILE_ARGS=()
i=0
if [ ${#FILES[@]} -gt 0 ]; then
  for f in "${FILES[@]}"; do
    if [ -n "${f}" ] && [ -f "${f}" ]; then
      FILE_ARGS+=(-F "files[${i}]=@${f};type=text/markdown")
      i=$((i+1))
      if [ ${i} -ge 10 ]; then
        break  # Discord の添付上限
      fi
    fi
  done
fi

if [ ${#FILE_ARGS[@]} -gt 0 ]; then
  RESP=$(curl -sS -X POST "${URL}" \
    -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
    -F "payload_json=${PAYLOAD};type=application/json" \
    "${FILE_ARGS[@]}")
else
  RESP=$(curl -sS -X POST "${URL}" \
    -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${PAYLOAD}")
fi

MSG_ID=$(printf '%s' "${RESP}" | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin); print(d.get("id",""))
except Exception:
  pass' 2>/dev/null || true)

if [ -n "${MSG_ID}" ]; then
  echo "OK message_id=${MSG_ID}"
  exit 0
fi

echo "ERROR: ${RESP}" >&2
exit 1
