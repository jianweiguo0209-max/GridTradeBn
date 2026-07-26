#!/bin/bash
cd /Users/thomaschang/Projects/GridTradeBi
A=data/score_research_2026-07-21/ablation
for st in s1 s2; do
  f=$A/eff1_${st}_results.txt; [ -f "$f" ] || continue
  S=$(echo $st | tr a-z A-Z)
  t=$(grep -acE "^$S/" $f); u=$(grep -oE "^$S/[A-Z0-9-]+: +[^ ]+" $f|sort -u|wc -l|tr -d ' ')
  arms=$(grep -oE '\([0-9]+臂×' $f | tail -1 | tr -dc 0-9)
  echo "$S ${t}/$((arms*10))  唯一$u $([ "$t" = "$u" ] && echo ✓ || echo '✗重复!')"
  grep -oE "^$S/[A-Z0-9-]+" $f | cut -d/ -f2 | sort | uniq -c | awk -v a=$arms '{printf "   %-8s %2d/%s\n",$2,$1,a}'
done
echo "进程: $(ps -eo command|grep -c '[e]ff1_chain_scan.py')扫描 $(pgrep -cf beam_v2_driver)驱动 $(pgrep -cf relay.py)接力 $(pgrep -cf 'watchdog.py')看门狗"
n=$(grep -cE 'RED|FATAL|ERR' $A/ALERTS.log 2>/dev/null)
echo "告警: $n 条累计"; grep -E 'RED|FATAL|ERR' $A/ALERTS.log 2>/dev/null | tail -3 | sed 's/^/   /'
