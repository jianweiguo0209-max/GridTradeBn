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
echo "进程: $(ps -eo command|grep -c '[e]ff1_chain_scan.py')扫描 $(pgrep -f beam_v2_driver|wc -l|tr -d " ")驱动 $(pgrep -f relay.py|wc -l|tr -d " ")接力 $(pgrep -f watchdog.py|wc -l|tr -d " ")看门狗"
new=$(awk '/MARK/{f=1;next} f' $A/ALERTS.log 2>/dev/null | grep -E 'RED|FATAL|ERR')
echo "新增告警: $(echo -n "$new" | grep -c . ) 条"; [ -n "$new" ] && echo "$new" | tail -4 || true | sed 's/^/   /'
