#!/bin/bash
# Add devices to an existing AMD installation with retries (see docs/remote-servers.md, "Adding devices later").
# A bad download ends the AMD batch installer; good files are kept, bad ones are removed and it runs again.
# Run it on the server, in the background:  nohup bash add_retry.sh > /dev/null 2>&1 < /dev/null &
# Edit the paths first. Writes add_attempts.log and add_attempt<N>.log next to add.log.
DIR=/mnt/perflab/benyamin/installers                              # installer folder (token in $DIR/home)
X=/home/slurmslave/benyamin/xilinx/.xinstall/2025.2.1               # xsetup of the installation itself
CONFIG=$DIR/home/.Xilinx/install_config_add.txt                    # the families to add set to :1
PAY=/home/slurmslave/benyamin/xilinx/Downloads/Vitis_2025.2.1/payload
L=$DIR/add.log
cd "$X" || exit 1
for i in 1 2 3 4 5 6 7 8; do
  echo "=== add attempt $i $(date)" >> "$DIR/add_attempts.log"
  _JAVA_OPTIONS=-Duser.home=$DIR/home ./xsetup -a XilinxEULA,3rdPartyEULA -b Add -c "$CONFIG" > "$L" 2>&1 < /dev/null
  cp "$L" "$DIR/add_attempt$i.log"
  if ! grep -q "DownloadFailed" "$L"; then
    echo "ENDED attempt $i without a download failure (check: tail add.log)" >> "$DIR/add_attempts.log"; exit 0
  fi
  for f in $(grep -oE "/[^<]*/Downloads/[^<]*\.xz" "$L"; grep -oE "filename=[^&]*\.xz" "$L" | sed "s|^filename=|$PAY/|"); do
    case "$f" in "$PAY"/*) rm -f "$f" && echo "removed $f" >> "$DIR/add_attempts.log";; esac
  done
done
echo "FAILED after 8 attempts" >> "$DIR/add_attempts.log"; exit 1
