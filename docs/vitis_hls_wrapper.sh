#!/bin/bash
# vitis_hls for Vitis 2025+: the standalone vitis_hls command is gone; `vitis_hls -f script.tcl` becomes
# `vitis-run --mode hls --tcl script.tcl`. Works wherever Vivado's or Vitis' settings64.sh was sourced (it uses
# $XILINX_VITIS), so the same file serves this machine and a server. See docs/remote-servers.md.
if [ -z "${XILINX_VITIS:-}" ] || [ ! -x "$XILINX_VITIS/bin/vitis-run" ]; then
  echo "vitis_hls: source the Vivado or Vitis settings64.sh first (XILINX_VITIS is not set)" >&2
  exit 127
fi
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -f) ARGS+=(--tcl "$2"); shift 2 ;;
    -version|--version) ARGS+=(--version); shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
exec "$XILINX_VITIS/bin/vitis-run" --mode hls "${ARGS[@]}"
