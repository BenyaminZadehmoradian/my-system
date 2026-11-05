#!/bin/bash
# AMD installer in batch mode with retries (see docs/remote-servers.md, section 3).
# Run it on the server, in the background:  nohup bash install_retry.sh > /dev/null 2>&1 < /dev/null &
# Edit the three paths first. Writes attempts.log and install_attempt<N>.log next to itself.
DIR=/mnt/perflab/benyamin/installers                  # where the unpacked installer and this script are
UNPACKED=$DIR/unified_2025.2.1                         # bash FPGAs_*_Lin64.bin --keep --noexec --target ...
CONFIG=$DIR/home/.Xilinx/install_config_local.txt      # Destination= on a local (case-sensitive) disk
L=$DIR/install.log
cd "$UNPACKED" || exit 1
for i in 1 2 3 4 5; do
  echo "=== attempt $i $(date)" >> "$DIR/attempts.log"
  _JAVA_OPTIONS=-Duser.home=$DIR/home ./xsetup -a XilinxEULA,3rdPartyEULA -b Install -c "$CONFIG" > "$L" 2>&1 < /dev/null
  cp "$L" "$DIR/install_attempt$i.log"
  if grep -qiE "Tool installation completed|installation completed successfully|Installation was successful" "$L"; then
    echo "SUCCESS attempt $i" >> "$DIR/attempts.log"; exit 0
  fi
  # a corrupted download: remove that file and try again (good files are kept and not downloaded again)
  bad=$(grep -oE "/[^<]*/Downloads/[^<]*\.xz" "$L" | sort -u)
  if [ -z "$bad" ]; then echo "FAILED attempt $i (not a bad download; see install_attempt$i.log)" >> "$DIR/attempts.log"; exit 1; fi
  for f in $bad; do rm -f "$f"; echo "removed $f" >> "$DIR/attempts.log"; done
done
echo "FAILED after 5 attempts" >> "$DIR/attempts.log"; exit 1
