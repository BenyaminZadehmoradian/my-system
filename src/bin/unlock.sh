#!/usr/bin/env bash
# Give code, rules and config back to you for editing. Run `ms lock` afterwards.
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
set -e
DIRS=()
for d in bin lib plugins gui ai runlog config systemd README.md; do [ -e "$MS/$d" ] && DIRS+=("$MS/$d"); done
sudo chown -R "$(id -u):$(id -g)" "${DIRS[@]}"
chmod -R u+w "${DIRS[@]}"
echo "unlocked — edit $MS/config/*, then run: ms lock"
