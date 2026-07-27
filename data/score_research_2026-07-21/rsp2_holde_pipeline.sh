#!/bin/bash
# HOLD-E 终裁流水线(2026-07-26,消融格战役 EP2/D_REP/D_ESP × 六链 = 18臂+锚)。
# warm(幂等复核) → stage L → stage F → POOL → MAIN 19臂 → 机械裁决。
# 串行单重进程;各步幂等,中断重跑本脚本即续。
# ⚠只应在预注册 c61c7ac 之后运行(主臂 EP2_s030 与部署门三条已冻结)。
#
# 注:POOL 走 rsp_final_bt.py —— 它才有 build_pool,且产物名 rsp_pool_HOLD-E.parquet
# 与 rsp2_final_bt.pool_path() 对 HOLD-E 的解析一致;rsp2 负责臂位/选币器/裁决。
set -u
cd /Users/thomaschang/Projects/GridTradeBi
A=data/score_research_2026-07-21/ablation
D=data/score_research_2026-07-21
PY=.venv/bin/python

echo "[pipe] $(date +%H:%M) warm 幂等复核(前次被中止令 kill,未写 DONE 标记)..."
BT_VISION_WORKERS=6 $PY -u "$D/rsp_warm_holde.py" > "$A/rsp_warm_holde.log" 2>&1
tail -2 "$A/rsp_warm_holde.log"

echo "[pipe] $(date +%H:%M) stage L 标签..."
HG_WORKERS=2 $PY -u "$D/holdout_gate.py" HOLD-E L > "$A/hg_HOLD-E_L.log" 2>&1
tail -2 "$A/hg_HOLD-E_L.log"

echo "[pipe] $(date +%H:%M) stage F 面板..."
HG_WORKERS=2 $PY -u "$D/holdout_gate.py" HOLD-E F > "$A/hg_HOLD-E_F.log" 2>&1
tail -2 "$A/hg_HOLD-E_F.log"

echo "[pipe] $(date +%H:%M) POOL(走 rsp_final_bt 的 build_pool)..."
BT_STAGE=POOL BT_WINDOWS=HOLD-E $PY -u "$D/rsp_final_bt.py" > "$A/rsp_pool_HOLD-E.log" 2>&1
grep -aE 'POOL/HOLD-E|DONE' "$A/rsp_pool_HOLD-E.log" | tail -2

echo "[pipe] $(date +%H:%M) MAIN 19臂(唯一裁决窗)..."
BT_STAGE=HOLD-E BT_WORKERS=3 $PY -u "$D/rsp2_final_bt.py" > "$A/rsp2_holde_main.log" 2>&1
grep -aE 'HOLD-E/|锚|preload' "$A/rsp2_holde_main.log" | tail -25

echo "[pipe] $(date +%H:%M) 机械裁决"
$PY "$D/rsp2_verdict.py" 2>&1 | tail -45
echo "RSP2_HOLDE_PIPELINE_DONE"
