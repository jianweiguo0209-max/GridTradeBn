#!/bin/bash
# HOLD-E 终裁流水线(2026-07-26,RSP111 战役)。
# 等下载 → stage L 标签 → stage F 面板 → POOL → MAIN(六臂) → 机械裁决。
# 全程串行(单重进程),16GB 机器安全;各步幂等,中断重跑本脚本即续。
# ⚠只应在预注册 664bc19 之后运行。
set -u
cd /Users/thomaschang/Projects/GridTradeBi
A=data/score_research_2026-07-21/ablation
D=data/score_research_2026-07-21
PY=.venv/bin/python

echo "[pipe] $(date +%H:%M) 等 HOLD-E 档案下载..."
until grep -q 'RSP_WARM_HOLDE_DONE' "$A/rsp_warm_holde.log" 2>/dev/null; do sleep 60; done
echo "[pipe] $(date +%H:%M) 下载完成"
grep -a 'DONE' "$A/rsp_warm_holde.log" | tail -1

echo "[pipe] $(date +%H:%M) stage L 标签..."
HG_WORKERS=2 $PY -u "$D/holdout_gate.py" HOLD-E L > "$A/hg_HOLD-E_L.log" 2>&1
tail -2 "$A/hg_HOLD-E_L.log"

echo "[pipe] $(date +%H:%M) stage F 面板..."
HG_WORKERS=2 $PY -u "$D/holdout_gate.py" HOLD-E F > "$A/hg_HOLD-E_F.log" 2>&1
tail -2 "$A/hg_HOLD-E_F.log"

echo "[pipe] $(date +%H:%M) POOL..."
BT_STAGE=POOL BT_WINDOWS=HOLD-E $PY -u "$D/rsp_final_bt.py" > "$A/rsp_pool_HOLD-E.log" 2>&1
grep -aE 'POOL/|DONE' "$A/rsp_pool_HOLD-E.log" | tail -2

echo "[pipe] $(date +%H:%M) MAIN 六臂(唯一裁决窗)..."
BT_STAGE=HOLD-E BT_WORKERS=3 $PY -u "$D/rsp_final_bt.py" > "$A/rsp_holde_main.log" 2>&1
grep -aE 'HOLD-E/|锚|preload' "$A/rsp_holde_main.log"

echo "[pipe] $(date +%H:%M) 机械裁决"
$PY "$D/rsp_report.py" 2>&1 | sed -n '/HOLD-E 六臂/,$p'
echo "RSP_HOLDE_PIPELINE_DONE"
