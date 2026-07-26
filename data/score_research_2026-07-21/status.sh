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
# ⚠ 进程计数必须限定在 **python 进程**上:pgrep -f 会匹配到父 shell
#   (本条命令的文本里若含脚本名,包裹它的 bash -c 就被数进去)——第三种自匹配变种。
#   (ps -eo comm 会截断到 16 字符,用不了;改看 command 的首字段=解释器路径)
cnt(){ ps -eo command | awk -v p="$1" '$1 ~ /[Pp]ython/ && index($0,p)>0' | wc -l | tr -d " "; }
echo "进程: $(cnt eff1_chain_scan.py)扫描 $(cnt beam_v2_driver.py)驱动 $(cnt relay.py)接力 $(cnt watchdog.py)看门狗"
new=$(awk '/MARK/{f=1;next} f' $A/ALERTS.log 2>/dev/null | grep -E 'RED|FATAL|ERR')
echo "新增告警: $(echo -n "$new" | grep -c . ) 条"; [ -n "$new" ] && echo "$new" | tail -4 || true | sed 's/^/   /'
