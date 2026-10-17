#!/usr/bin/env bash
# Компактная сводка для экономного мониторинга: треки, открытые PR, отчёты ревью. Ничего не меняет.
#   sweep.sh [каталог worktree, по умолчанию ~/dev-worktrees]
set -uo pipefail
WT="${1:-$HOME/dev-worktrees}"; MAIN="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
echo "== $(date '+%H:%M') агенты: agy $(pgrep -f agy_supervise.sh | wc -l | tr -d ' '), dsh $(pgrep -f 'dsh.*--profile' | wc -l | tr -d ' ')"
for d in $(find "$WT" -maxdepth 1 -mindepth 1 -type d -mmin -${SWEEP_RECENT_MIN:-240}); do d="$d/"
  [ -d "$d/.git" ] || [ -f "$d/.git" ] || continue
  printf '%-10s %-34s %s\n' "$(basename "$d")" "$(git -C "$d" log -1 --format=%s | cut -c1-34)" "dirty=$(git -C "$d" status --short | grep -vc '^??')"
done | awk '$0 !~ /бриф задачи/ || 1' | head -40
echo "== PR:"; gh pr list --state open --json number,title,mergeStateStatus --jq '.[] | "#\(.number) \(.mergeStateStatus) \(.title[0:60])"' 2>/dev/null
echo "== ревью:"; ls -1t "$MAIN/.agents/reviews/"*.txt 2>/dev/null | head -8 | while read -r f; do echo "$(basename "$f"): $(grep -E 'ЕСТЬ ЗАМЕЧАНИЯ|OK$' "$f" | tail -1 | cut -c1-40)"; done
