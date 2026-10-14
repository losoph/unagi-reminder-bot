#!/usr/bin/env bash
# Создание и удаление изолированных деревьев для параллельных исполнителей и ревьюеров.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(git -C "$PWD" rev-parse --show-toplevel)"
WORKTREES="${AGENT_WORKTREES:-$HOME/dev-worktrees}"
mkdir -p "$WORKTREES"

usage() { echo "использование: worktree.sh new <name> <branch> [base] | review <name> <commit> | rm <name>" >&2; exit 2; }
[[ $# -ge 2 ]] || usage
cmd="$1"; name="$2"
case "$cmd" in
  new)
    [[ $# -ge 3 && $# -le 4 ]] || usage
    branch="$3"; base="${4:-HEAD}"; path="$WORKTREES/$name"
    [[ "$name" != */* && "$name" != . && "$name" != .. ]] || { echo "недопустимое имя: $name" >&2; exit 2; }
    git -C "$ROOT" worktree add -b "$branch" "$path" "$base"
    git -C "$path" config core.fsmonitor false
    echo "$path"
    ;;
  review)
    [[ $# -eq 3 ]] || usage
    path="$WORKTREES/$name"
    [[ "$name" != */* && "$name" != . && "$name" != .. ]] || { echo "недопустимое имя: $name" >&2; exit 2; }
    git -C "$ROOT" worktree add --detach "$path" "$3"
    git -C "$path" config core.fsmonitor false
    echo "$path"
    ;;
  rm)
    [[ $# -eq 2 ]] || usage
    [[ "$name" != */* && "$name" != . && "$name" != .. ]] || { echo "недопустимое имя: $name" >&2; exit 2; }
    git -C "$ROOT" worktree remove "$WORKTREES/$name"
    ;;
  *) usage ;;
esac
