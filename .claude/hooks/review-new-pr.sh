#!/usr/bin/env bash
# PostToolUse hook (Bash): after Claude runs `gh pr create` and it prints a PR
# URL, tell Claude to review that PR in the same session.
set -euo pipefail

input=$(cat)

cmd=$(jq -r '.tool_input.command // ""' <<<"$input")
case "$cmd" in
  *"gh pr create"*) ;;
  *) exit 0 ;;
esac

# Look for the PR URL anywhere in the tool response (stdout/stderr shapes vary).
url=$(jq -r '.tool_response | tostring' <<<"$input" \
  | grep -Eo 'https://github\.com/[^/[:space:]"]+/[^/[:space:]"]+/pull/[0-9]+' \
  | head -n 1 || true)
[ -n "$url" ] || exit 0

num=${url##*/}
repo=$(sed -E 's#https://github\.com/([^/]+/[^/]+)/pull/.*#\1#' <<<"$url")

msg="PR #${num} was just created: ${url}. Before doing anything else, review it for correctness bugs: \
read the diff with \`gh pr diff ${num} -R ${repo}\`, \
follow the \"Reviewing pull requests\" section of AGENTS.md, and report the findings to the user. \
Do not post comments on the PR or push fixes unless the user asks."

jq -n --arg ctx "$msg" --arg sys "Auto code review queued for PR #${num}" \
  '{systemMessage: $sys, hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $ctx}}'
