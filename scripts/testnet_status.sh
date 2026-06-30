#!/usr/bin/env bash
# testnet 运行状态一键快照：fly 机器状态 + 库内健康（心跳/标志/网格/指令/余额）。
# 只读，不改任何状态。用法：
#   bash scripts/testnet_status.sh          # 默认 app=gridtrade-hl
#   FLY_APP=gridtrade-hl bash scripts/testnet_status.sh
set -euo pipefail
APP="${FLY_APP:-gridtrade-hl}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "########## fly machines ($APP) ##########"
fly status -a "$APP" | sed -n '1,20p'

echo
echo "########## db + exchange health ##########"
# 经 stdin 把只读快照脚本喂给机器上的 python（复用机器 env 的 DATABASE_URL/凭证）
cat "$HERE/testnet_status.py" | fly ssh console -a "$APP" -C python
