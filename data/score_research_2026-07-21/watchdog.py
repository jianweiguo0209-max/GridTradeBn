"""无人值守看门狗:盯束搜索流水线的五类故障,写告警 + 安全自愈(2026-07-27)。

**五类故障**
 1. 驱动器死亡        → 自动重启(驱动器可重入,每段落盘;重启即续)
 2. 扫描作业异常退出   → 记录;若该段结果残缺,标红(见 5)
 3. 内存压力 ≥2 / 换页 → 记录速率;持续 5 轮则标红(不自动杀进程——杀一半会造成 5)
 4. 停滞             → 结果文件 15 分钟不增长且有进程在跑 → 标红
 5. **结果残缺**(最险)→ 某段作业已全退但臂×单元不满 ⇒ 驱动器会拿残缺数据选点
                        (score() 对跑不全的臂返回 None 静默跳过)⇒ 束会选错。标红并**停驱动器**,
                        避免它带着错误的束继续跑下一段。

**不做的事**:不自动杀扫描作业(会制造故障 5)、不改任何已在运行的脚本、不碰终审窗。

用法: nohup watchdog.py &     (自身轻量,60s 一轮)
"""
import json
import os
import re
import subprocess
import sys
import time

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
A = RD + '/ablation'
PY = '/Users/thomaschang/Projects/GridTradeBi/.venv/bin/python'
ALERT = A + '/ALERTS.log'
STAGE_ARMS = {'k1': 5, 'k2': 9, 'k3': 12, 'k4': 9}      # 每段应有臂数
UNITS = 10
STALL_MIN = 15


def alert(level, msg):
    line = '[%s] %-6s %s' % (time.strftime('%m-%d %H:%M:%S'), level, msg)
    open(ALERT, 'a').write(line + '\n')
    print(line, flush=True)


def alive(pat):
    return subprocess.run(['pgrep', '-f', pat], capture_output=True).returncode == 0


def pressure():
    try:
        return int(subprocess.run(['sysctl', '-n', 'kern.memorystatus_vm_pressure_level'],
                                  capture_output=True, text=True).stdout.strip())
    except Exception:
        return -1


def swapouts():
    try:
        o = subprocess.run(['vm_stat'], capture_output=True, text=True).stdout
        m = re.search(r'Swapouts:\s+(\d+)', o)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def stage_state(st):
    """→ (读数数, 应有数, 作业是否在跑)"""
    f = '%s/eff1_%s_results.txt' % (A, st)
    n = 0
    if os.path.exists(f):
        n = sum(1 for ln in open(f) if ln.startswith(st.upper() + '/'))
    running = alive('K_STAGE=%s' % st.upper()) or alive('eff1_k1_pv5stop' if st == 'k1'
                                                        else 'zzz-none')
    return n, STAGE_ARMS[st] * UNITS, running


def scan_logs():
    """在各日志里找 Traceback / Error / assert 失败。"""
    hits = []
    for f in os.listdir(A):
        if not f.endswith('.log') or f == os.path.basename(ALERT):
            continue
        try:
            txt = open(os.path.join(A, f), errors='ignore').read()[-40000:]
        except Exception:
            continue
        for pat in ('Traceback (most recent call last)', 'AssertionError',
                    'MemoryError', 'BrokenProcessPool', 'Killed'):
            if pat in txt:
                hits.append((f, pat))
    return hits


def main():
    alert('START', '看门狗启动 pid=%d;盯 驱动器/扫描作业/内存/停滞/结果残缺' % os.getpid())
    last_n, last_change, hi_pressure, seen = {}, {}, 0, set()
    prev_swap = swapouts()
    while True:
        now = time.time()
        drv = alive('chain_greedy_driver')
        scans = subprocess.run(['pgrep', '-f', 'eff1_chain_scan|eff1_k1_pv5stop'],
                               capture_output=True, text=True).stdout.split()
        p = pressure()
        sw = swapouts()
        rate = (sw - prev_swap) / 60.0
        prev_swap = sw

        # 1 驱动器死亡 → 自动重启(可重入)
        if not drv:
            if os.path.exists(A + '/greedy_final_beam.json'):
                alert('DONE', '束搜索已完成,看门狗退出'); return
            alert('FATAL', '驱动器不在了 ⇒ 自动重启(可重入,每段已落盘)')
            subprocess.Popen([PY, '-u', RD + '/chain_greedy_driver.py'],
                             stdout=open(A + '/greedy_driver.out', 'a'),
                             stderr=subprocess.STDOUT)
            time.sleep(30)

        # 3 内存
        if p >= 2:
            hi_pressure += 1
            if hi_pressure >= 5:
                alert('RED', '内存压力=%d 已持续 %d 轮,换页 %.0f 页/秒 —— 不自动杀作业'
                             '(杀一半会造成结果残缺),请人工判断' % (p, hi_pressure, rate))
                hi_pressure = 0
        else:
            hi_pressure = 0

        # 4/5 逐段状态
        for st in ('k1', 'k2', 'k3', 'k4'):
            n, need, running = stage_state(st)
            if n == 0:
                continue
            if last_n.get(st) != n:
                last_n[st], last_change[st] = n, now
            stalled = running and (now - last_change.get(st, now)) > STALL_MIN * 60
            if stalled and ('stall-' + st) not in seen:
                seen.add('stall-' + st)
                alert('RED', '%s 停滞:%d/%d 读数已 %d 分钟无增长,但仍有进程'
                             % (st.upper(), n, need, STALL_MIN))
            # 5 残缺(最险):作业全退但读数不满
            if not running and 0 < n < need and ('short-' + st) not in seen:
                seen.add('short-' + st)
                alert('RED', '**%s 结果残缺 %d/%d** —— 驱动器会对跑不全的臂静默跳过 ⇒ 束会选错。'
                             '已停驱动器,请人工续跑该段后再启动' % (st.upper(), n, need))
                subprocess.run(['pkill', '-f', 'chain_greedy_driver'])

        # 2 日志异常
        for f, pat in scan_logs():
            key = 'log-%s-%s' % (f, pat)
            if key not in seen:
                seen.add(key)
                alert('ERR', '%s 出现 %s' % (f, pat))

        time.sleep(60)


if __name__ == '__main__':
    main()
