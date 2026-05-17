#!/bin/bash
# switch_profile.sh: publish.py / fetch_metrics.py の --profile を一括切替
#
# 使い方:
#   ./switch_profile.sh test
#   ./switch_profile.sh production
#
# 動作:
#   1. repo/01_active/launchd/ の 4 plist を書き換え（publish×3 + metrics×1）
#   2. ~/Library/LaunchAgents/ にコピー
#   3. launchctl bootout → bootstrap で再ロード
#
# 注意:
#   - production 切替時は確認プロンプトあり
#   - weekly_summary.plist が追加されたらこの PLISTS 配列に追加すること

set -euo pipefail

NEW_PROFILE="${1:-}"
case "${NEW_PROFILE}" in
  test|production) ;;
  *)
    echo "Usage: $0 {test|production}" >&2
    exit 1
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_LAUNCHD="${SCRIPT_DIR}/../launchd"
LA_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"

PLISTS=(
  "com.example.threads_auto.publish_morning"
  "com.example.threads_auto.publish_noon"
  "com.example.threads_auto.publish_evening"
  "com.example.threads_auto.metrics"
)

if [ "${NEW_PROFILE}" = "production" ]; then
  printf '⚠️  本番アカウント @${THREADS_USERNAME} に切り替えます。続行しますか？ (yes/N): '
  read -r ans
  [ "${ans}" = "yes" ] || { echo "aborted"; exit 1; }
fi

echo "=== profile を ${NEW_PROFILE} に切り替え ==="

for label in "${PLISTS[@]}"; do
  plist="${REPO_LAUNCHD}/${label}.plist"
  if [ ! -f "${plist}" ]; then
    echo "missing: ${plist}" >&2
    exit 1
  fi

  # --profile の次の <string>...</string> を置換
  python3 - "${plist}" "${NEW_PROFILE}" <<'PY'
import sys, re
path, new = sys.argv[1], sys.argv[2]
with open(path) as f: text = f.read()
new_text, n = re.subn(
    r'(<string>--profile</string>\s*\n\s*<string>)(test|production)(</string>)',
    lambda m: m.group(1) + new + m.group(3),
    text,
)
if n != 1:
    sys.exit(f"unexpected match count={n} in {path}")
with open(path, 'w') as f: f.write(new_text)
print(f"  updated repo: {path}")
PY

  # LaunchAgents にコピー
  cp "${plist}" "${LA_DIR}/${label}.plist"
  echo "  copied to LaunchAgents: ${label}"

  # 既存をアンロード → ロード
  if launchctl print "gui/${UID_NUM}/${label}" >/dev/null 2>&1; then
    # 新しい構文（gui/uid/label）→ 古い構文（gui/uid plist_path）の順で試す
    launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null || \
      launchctl bootout "gui/${UID_NUM}" "${LA_DIR}/${label}.plist" 2>/dev/null || \
      true
  fi
  launchctl bootstrap "gui/${UID_NUM}" "${LA_DIR}/${label}.plist"
  echo "  bootstrapped: ${label}"
  echo ""
done

echo "✅ profile を ${NEW_PROFILE} に切り替えました（${#PLISTS[@]} plist）"
echo ""
echo "確認コマンド:"
echo "  for l in ${PLISTS[*]}; do"
echo "    echo \"--- \$l ---\""
echo "    grep -A1 -- '--profile' ~/Library/LaunchAgents/\${l}.plist | tail -2"
echo "  done"
