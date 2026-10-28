#!/usr/bin/env bash
# Record the current content of every watched file as the known-good baseline.
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
db="$MS/state/watched.sha256"
mkdir -p "$MS/state"
[ -f "$db" ] && chmod u+w "$db"
: > "$db"
grep -v '^\s*#' "$MS/config/watched_files.conf" 2>/dev/null | sed "s|^~|$HOME|" | while read -r f; do
  [ -n "$f" ] && [ -r "$f" ] && sha256sum "$f" >> "$db"
done
chmod a-w "$db"
echo "baseline saved: $(wc -l < "$db") files"
