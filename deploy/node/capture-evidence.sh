#!/usr/bin/env bash
# Evidence-capture harness for the MazaPay node deployment.
# Usage: capture-evidence.sh <outfile> <label> -- command [args...]
# Wraps every capture with host identity, UTC timestamp, the exact command,
# its full output, and its exit code - so any transcript in this artifact is
# self-contained and independently reproducible.
set -u
if [ "$#" -lt 3 ] || [ "${2:-}" = "--" ]; then
  echo "usage: capture-evidence.sh <outfile> <label> -- command [args...]" >&2
  exit 64
fi
OUT="$1"; LABEL="$2"; shift 2
[ "${1:-}" = "--" ] && shift
{
  echo "### LABEL: $LABEL"
  echo "### HOST: $(hostname 2>/dev/null || echo unknown)"
  echo "### UTC: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "### KERNEL: $(uname -srmo 2>/dev/null || echo unknown)"
  echo
  echo "### CMD:"
  printf '%s ' "$@"; echo
  echo
  echo "### OUTPUT:"
  "$@" 2>&1
  RC=$?
  echo
  echo "### EXIT: $RC"
} > "$OUT" 2>&1
exit "$RC"
