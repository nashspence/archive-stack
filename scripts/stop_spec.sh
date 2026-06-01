#!/usr/bin/env bash
set -euo pipefail

PATTERN="tests/harness/test_spec_harness.py"
STOP_GRACE_SECONDS="${STOP_GRACE_SECONDS:-5}"

pids=()
while IFS= read -r pid; do
    if [[ -z "${pid}" || "${pid}" == "$$" || "${pid}" == "${PPID}" ]]; then
      continue
    fi
    pids+=("${pid}")
done < <(pgrep -f "${PATTERN}" || true)

if [[ "${#pids[@]}" -eq 0 ]]; then
  printf 'No in-flight spec harness process found.\n'
  exit 0
fi

printf 'Stopping spec harness process(es): %s\n' "${pids[*]}"
kill -INT "${pids[@]}" 2>/dev/null || true

deadline=$((SECONDS + STOP_GRACE_SECONDS))
while [[ "${#pids[@]}" -gt 0 && "${SECONDS}" -lt "${deadline}" ]]; do
  remaining=()
  for pid in "${pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      remaining+=("${pid}")
    fi
  done
  pids=("${remaining[@]}")
  if [[ "${#pids[@]}" -gt 0 ]]; then
    sleep 0.2
  fi
done

if [[ "${#pids[@]}" -gt 0 ]]; then
  printf 'Spec harness did not exit after SIGINT; sending SIGTERM to: %s\n' "${pids[*]}" >&2
  kill -TERM "${pids[@]}" 2>/dev/null || true
fi
