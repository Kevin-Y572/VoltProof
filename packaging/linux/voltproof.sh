#!/bin/sh
# VoltProof 的 Linux 启动器。ngspice 由软件包依赖提供，不打进本包。
# KiCad、Java、Freerouting 是可选的 PCB 工具，也不打进本包。
set -eu
if [ -z "${HOME:-}" ]; then
  echo "voltproof: HOME 未设置，无法选择数据目录" >&2
  exit 1
fi
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/voltproof"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/voltproof"
mkdir -p "$DATA" "$CONFIG"
if [ -f "$CONFIG/env" ]; then
  # 用户自己的 shell 片段，用来放 VOLTPROOF_KICAD / VOLTPROOF_FREEROUTING /
  # VOLTPROOF_JAVA / VOLTPROOF_NGSPICE。
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG/env"
  set +a
fi
export PYTHONNOUSERSITE=1
export PYTHONPATH="/opt/voltproof/lib${PYTHONPATH:+:$PYTHONPATH}"
export VOLTPROOF_WORKSPACE="${VOLTPROOF_WORKSPACE:-$DATA}"
export VOLTPROOF_SETTINGS="${VOLTPROOF_SETTINGS:-$CONFIG/settings.json}"
cd "$DATA"
exec /usr/bin/python3 -m app.cli "$@"
