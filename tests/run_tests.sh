#!/bin/bash
# Run every test of my-system on this machine (no pytest needed; nothing real is touched: each test uses its own
# temporary queue and, for servers, a simulated Slurm cluster).
#   bash tests/run_tests.sh              all tests (~5 min)
#   bash tests/run_tests.sh v18 remote   only tests/test_v18.py and tests/test_remote.py
# The live tests against the real Titan are separate: see tests/live/README.md.
cd "$(dirname "$0")" || exit 2
files=()
if [ $# -gt 0 ]; then for a in "$@"; do files+=("test_${a#test_}"); files[-1]="${files[-1]%.py}.py"; done
else files=(test_*.py); fi
pass=0; fail=0; failed=()
t0=$(date +%s)
for f in "${files[@]}"; do
  [ -f "$f" ] || { echo "no such test file: $f"; fail=$((fail + 1)); failed+=("$f"); continue; }
  printf '%-24s ' "$f"
  log=$(mktemp)
  if MS_NO_CGROUP=1 python3 "$f" > "$log" 2>&1; then
    echo "PASS ($(grep -c '^ok ' "$log") tests)"; pass=$((pass + 1))
  else
    echo "FAIL"; fail=$((fail + 1)); failed+=("$f"); sed 's/^/    | /' "$log" | tail -25
  fi
  rm -f "$log"
done
echo
echo "$pass file(s) passed, $fail failed · $(( $(date +%s) - t0 )) s"
[ $fail = 0 ] || { echo "failed: ${failed[*]}"; exit 1; }
