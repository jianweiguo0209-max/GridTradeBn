#!/bin/bash
# p12 战役编排(2026-07-25 v2):等档案补全 → 标签重建 ∥ POOL → MAIN → HOLD。
#
# v2 增标签重建(关键修复):cross1 读 1m 算,旧标签币数=当时有 1m 的币数(老窗缺 17~26%)。
# 补了 1m 却不重建标签,p12 臂仍只能在旧币集里选 = 补全白做。HOLD-C/D 系补全后新建,跳过。
#
# 并发(16GB 硬约束):标签串行 1 进程 ∥ POOL 每批 2 窗;标签是 MAIN 的硬前提,先于 MAIN 完成。
# 续跑:POOL 轮级 ckpt、MAIN/HOLD 臂窗级、标签文件级——直接重跑本脚本即可接上。
set -u
cd /Users/thomaschang/Projects/GridTradeBi
A=data/score_research_2026-07-21/ablation
PY=.venv/bin/python
BT=data/score_research_2026-07-21/p12_final_bt.py
HG=data/score_research_2026-07-21/holdout_gate.py

echo "[orch] $(date +%H:%M) 等档案补全 P12_WARM_ALL_DONE..."
until grep -q 'P12_WARM_ALL_DONE' "$A/p12_warm_all.log" 2>/dev/null; do sleep 60; done
echo "[orch] $(date +%H:%M) 补全完成,开跑"

# --- 标签重建(串行,后台;判定六窗。HOLD-C/D 已是补全后产物) ---
(
  for w in HOLD-B HOLD-A W1 W2 OOS IS; do
    echo "[lab] $(date +%H:%M) $w 开建"
    HG_WORKERS=2 $PY -u "$HG" "$w" L > "$A/hg_$w.log" 2>&1
    echo "[lab] $(date +%H:%M) $w $(tail -2 "$A/hg_$w.log" | head -1)"
  done
  echo "[lab] LABELS_DONE"
) > "$A/p12_labels.log" 2>&1 &
LAB_PID=$!

run_batch() {                      # $@ = 窗名列表,并行跑 POOL
  local pids=()
  for w in "$@"; do
    BT_STAGE=POOL BT_WINDOWS="$w" nohup $PY -u "$BT" > "$A/p12_pool_$w.log" 2>&1 &
    pids+=($!)
    echo "[orch] POOL $w pid=$!"
    sleep 5
  done
  for p in "${pids[@]}"; do wait "$p"; done
  echo "[orch] $(date +%H:%M) 批完成: $*"
}

run_batch HOLD-B HOLD-A
run_batch HOLD-C HOLD-D
run_batch W1 W2
run_batch OOS IS
echo "[orch] $(date +%H:%M) 全部 POOL 完成"

echo "[orch] 等标签重建收尾..."
wait $LAB_PID
echo "[orch] $(date +%H:%M) 标签就绪"

# --- MAIN/HOLD:引擎阶段,2 窗并行(preload 载 1m 是内存大头) ---
for pair in "HOLD-B HOLD-A" "W1 W2" "OOS IS"; do
  set -- $pair
  BT_STAGE=MAIN BT_WINDOWS="$1" BT_WORKERS=3 nohup $PY -u "$BT" > "$A/p12_main_$1.log" 2>&1 &
  p1=$!
  sleep 5
  BT_STAGE=MAIN BT_WINDOWS="$2" BT_WORKERS=3 nohup $PY -u "$BT" > "$A/p12_main_$2.log" 2>&1 &
  p2=$!
  wait $p1; wait $p2
  echo "[orch] $(date +%H:%M) MAIN 完成: $pair"
done

BT_STAGE=HOLD BT_WINDOWS=HOLD-C BT_WORKERS=3 nohup $PY -u "$BT" > "$A/p12_hold_C.log" 2>&1 &
p1=$!
sleep 5
BT_STAGE=HOLD BT_WINDOWS=HOLD-D BT_WORKERS=3 nohup $PY -u "$BT" > "$A/p12_hold_D.log" 2>&1 &
p2=$!
wait $p1; wait $p2
echo "[orch] $(date +%H:%M) HOLD 完成"
echo "P12_ORCH_DONE"
