#!/usr/bin/env bash
# 解析 session-digger 根目录并 echo 到 stdout。
# 用法: SD_ROOT="$(scripts/resolve_root.sh)"
# 探测优先级: SESSION_DIGGER_ROOT → 宿主插件根 → 相对本脚本 → 常见 skill 安装位。
# 多副本并存时务必 export SESSION_DIGGER_ROOT。
set -euo pipefail

SD_ROOT="${SESSION_DIGGER_ROOT:-${CLAUDE_PLUGIN_ROOT:-${HERDR_PLUGIN_ROOT:-}}}"
if [[ -z "$SD_ROOT" ]]; then
  SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
fi
if [[ -z "$SD_ROOT" || ! -f "$SD_ROOT/scripts/sd-recall.py" ]]; then
  for _c in \
    "$HOME/.agents/skills/session-digger" \
    "$HOME/.claude/plugins/session-digger" \
    "$HOME/.claude/skills/session-digger" \
    "$HOME/.grok/skills/session-digger"
  do
    [[ -f "$_c/scripts/sd-recall.py" ]] && SD_ROOT="$_c" && break
  done
fi
if [[ -z "$SD_ROOT" || ! -f "$SD_ROOT/scripts/sd-recall.py" ]]; then
  echo "resolve_root: session-digger root not found" >&2
  exit 1
fi
printf '%s\n' "$SD_ROOT"
