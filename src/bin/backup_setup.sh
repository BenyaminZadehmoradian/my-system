#!/usr/bin/env bash
# One-time backup setup: choose a repository location, store the passphrase in the desktop
# keyring, create the encrypted borg repository, save a key copy, enable nightly backups.
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
set -e
# shellcheck source=/dev/null
. "$MS/config/backup.conf"
command -v borg >/dev/null || { echo "borg is not installed (sudo apt install borgbackup)"; exit 1; }
command -v secret-tool >/dev/null || { echo "secret-tool is missing (sudo apt install libsecret-tools)"; exit 1; }

[ -z "${REPO:-}" ] && REPO=$(cat "$MS/state/backup_repo" 2>/dev/null)
if [ -z "$REPO" ]; then
  first_share=$(echo "$NET_SHARES" | awk '{print $1}')
  first_disk=$(echo "$DATA_DISKS" | awk '{print $1}')
  def="${first_share:-$first_disk}"; def="${def:+$def/backup_$USER}"
  echo "Where should the encrypted backup live? Best: a disk or share that is NOT this computer's root disk."
  [ -n "$first_share" ] && echo "  network share: $first_share"
  [ -n "$first_disk" ] && echo "  data disk:     $first_disk"
  read -rp "Repository path [${def:-none}]: " REPO
  REPO="${REPO:-$def}"
  [ -n "$REPO" ] || { echo "no path given"; exit 1; }
  echo "$REPO" > "$MS/state/backup_repo"
fi
export BORG_REPO="$REPO"

if secret-tool lookup $KEYRING_ATTR >/dev/null 2>&1; then
  echo "Passphrase already in the keyring — reusing it."
else
  echo "Choose a strong backup passphrase and SAVE IT somewhere off this computer."
  echo "Without it the backup can never be opened."
  while :; do
    read -rsp "Passphrase: " p1; echo
    read -rsp "Repeat:     " p2; echo
    [ -n "$p1" ] && [ "$p1" = "$p2" ] && break
    echo "Empty or not matching — try again."
  done
  printf '%s' "$p1" | secret-tool store --label="my-system borg backup" $KEYRING_ATTR
  unset p1 p2
fi
export BORG_PASSCOMMAND="secret-tool lookup $KEYRING_ATTR"

if [ -d "$REPO" ] && [ -f "$REPO/config" ]; then
  echo "Repository exists: $REPO"
else
  mkdir -p "$(dirname "$REPO")"
  borg init --encryption=repokey-blake2 "$REPO"
fi
borg key export --paper "$REPO" "$MS/state/borg-key-backup.txt" && chmod 600 "$MS/state/borg-key-backup.txt"
echo "Key copy: $MS/state/borg-key-backup.txt — keep a copy off this computer too."
touch "$MS/state/backup_configured"

systemctl --user daemon-reload
systemctl --user enable --now my-system-backup.timer my-system-verify.timer
echo "Nightly backup: $(systemctl --user list-timers my-system-backup.timer --no-legend | awk '{print $1, $2, $3}')"
if [ ! -f "$MS/state/backup_status" ]; then
  read -rp "Start the first backup now in the background? [Y/n] " a
  case "${a:-Y}" in n|N) ;; *) systemctl --user start --no-block my-system-backup.service; echo "Started.";; esac
fi
