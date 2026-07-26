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
UNITS = 10
STALL_MIN = 15
_HDR = re.compile(r'\((\d+)臂×(\d+)单元\)')


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
    """→ (读数数, 应有数, 作业是否在跑)

    ⚠ 应有数**不能写死** —— 束搜索每段的臂数取决于束宽与去重结果(S2 可能是 12~39 任意值),
    写死会让 `not running and n < need` 误判成"结果残缺"并**杀掉驱动器**。
    改从扫描器自己写的抬头 `(N臂×M单元)` 读**臂数**,取最后一次(即当前段)。
    ⚠ 抬头里的「单元」是**该作业分到的窗数(5)**,不是全段的;两作业各写一份抬头。
       全段单元数恒为 UNITS=10(SPLIT_A 5 窗 + SPLIT_B 5 窗),故用 臂数 × UNITS。
    """
    f = '%s/eff1_%s_results.txt' % (A, st)
    n, need = 0, None
    if os.path.exists(f):
        for ln in open(f):
            if ln.startswith(st.upper() + '/'):
                n += 1
            else:
                m = _HDR.search(ln)
                if m:
                    need = int(m.group(1)) * UNITS
    if need is None:
        return n, None, False
    # ⚠ 不能用 pgrep 匹配 K_STAGE —— **环境变量不在命令行里**,永远匹配不到,
    #   会把正在跑的段误判成"结果残缺"并杀掉驱动器(2026-07-27 实错,引发三重级联)。
    #   改用「结果文件最近 10 分钟内被写过」+「有扫描进程」双条件。
    fresh = os.path.exists(f) and (time.time() - os.path.getmtime(f)) < 600
    any_scan = alive('eff1_chain_scan')
    return n, need, (fresh and any_scan)


# ⚠ 只扫**本流水线**的日志:ablation/ 下有几周前旧战役的日志,里面的历史 traceback
#   会淹掉真告警(实测一启动就冒 6 条假告警)。且只看**启动之后新增**的内容。
LOG_PAT = re.compile(r'^(s[12]_[ab]\.log|beam_v2\.(log|out))$')
_pos = {}


def scan_logs():
    """只在本流水线日志的**新增部分**里找异常。"""
    hits = []
    for f in os.listdir(A):
        if not LOG_PAT.match(f):
            continue
        fp = os.path.join(A, f)
        try:
            sz = os.path.getsize(fp)
            start = _pos.get(f)
            if start is None:                  # 首轮:记录当前位置,不回溯历史
                _pos[f] = sz
                continue
            if sz <= start:
                _pos[f] = sz
                continue
            with open(fp, errors='ignore') as fh:
                fh.seek(start)
                txt = fh.read()
            _pos[f] = sz
        except Exception:
            continue
        for pat in ('Traceback (most recent call last)', 'AssertionError',
                    'MemoryError', 'BrokenProcessPool', 'Killed', 'FAIL'):
            if pat in txt:
                hits.append((f, pat))
    return hits


def main():
    alert('START', '看门狗启动 pid=%d;盯 驱动器/扫描作业/内存/停滞/结果残缺' % os.getpid())
    last_n, last_change, hi_pressure, seen = {}, {}, 0, set()
    prev_swap = swapouts()
    while True:
        now = time.time()
        drv = alive('beam_v2_driver')
        scans = subprocess.run(['pgrep', '-f', 'eff1_chain_scan|eff1_k1_pv5stop'],
                               capture_output=True, text=True).stdout.split()
        p = pressure()
        sw = swapouts()
        rate = (sw - prev_swap) / 60.0
        prev_swap = sw

        # 1 驱动器死亡 → 自动重启(可重入)
        # ⚠ 但**接力器在跑时驱动器的缺席是有意的** —— 接力器正等上一批扫描作业退出后
        #   才放驱动器。此时贸然重启会让驱动器立刻再起一对扫描作业,与在跑的那对
        #   **并发写同一结果文件**(2026-07-27 实错:产生 11 条重复读数)。
        if not drv and alive('relay.py'):
            time.sleep(60); continue
        if not drv:
            if os.path.exists(A + '/beam_v2_final.json'):
                alert('DONE', '束搜索已完成,看门狗退出'); return
            alert('FATAL', '驱动器不在了 ⇒ 自动重启(可重入,每段已落盘)')
            subprocess.Popen([PY, '-u', RD + '/beam_v2_driver.py'],
                             stdout=open(A + '/beam_v2.out', 'a'),
                             stderr=subprocess.STDOUT)
            time.sleep(30)

        # 3 内存。⚠ 光看 pressure=2 会误报:macOS 在有大量文件缓存时长期报 2,
        #   真正的危险信号是**实际换页**。要求 rate>0 才计数。
        if p >= 2 and rate > 0:
            hi_pressure += 1
            if hi_pressure >= 5:
                alert('RED', '内存压力=%d 已持续 %d 轮,换页 %.0f 页/秒 —— 不自动杀作业'
                             '(杀一半会造成结果残缺),请人工判断' % (p, hi_pressure, rate))
                hi_pressure = 0
        else:
            hi_pressure = 0

        # 4/5 逐段状态
        for st in ('s1', 's2'):
            n, need, running = stage_state(st)
            if n == 0 or not need:
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
                subprocess.run(['pkill', '-f', 'beam_v2_driver'])

        # 2 日志异常
        for f, pat in scan_logs():
            key = 'log-%s-%s' % (f, pat)
            if key not in seen:
                seen.add(key)
                alert('ERR', '%s 出现 %s' % (f, pat))

        time.sleep(60)


if __name__ == '__main__':
    main()
