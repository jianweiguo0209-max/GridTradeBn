"""接力器:等指定 PID 全部退出,再启动束搜索驱动器。

**为什么不用 pgrep**:pgrep -f 会匹配到接力器自己的命令行(里面含被搜的字符串),
造成自匹配、永远等不到退出(2026-07-27 实错)。改为对显式 PID 列表用 os.kill(pid,0) 轮询。
用法: relay.py <pid> [<pid> ...]
"""
import os, subprocess, sys, time

RD = '/Users/thomaschang/Projects/GridTradeBi/data/score_research_2026-07-21'
PY = '/Users/thomaschang/Projects/GridTradeBi/.venv/bin/python'
LOG = RD + '/ablation/beam_v2.out'


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def main():
    pids = [int(x) for x in sys.argv[1:]]
    with open(LOG, 'a') as f:
        f.write('[%s] 接力器 pid=%d 等待 %s 退出\n'
                % (time.strftime('%H:%M:%S'), os.getpid(), pids)); f.flush()
        while any(alive(p) for p in pids):
            time.sleep(30)
        f.write('[%s] 全部退出 → 启动驱动器\n' % time.strftime('%H:%M:%S')); f.flush()
    subprocess.run([PY, '-u', RD + '/beam_v2_driver.py'],
                   stdout=open(LOG, 'a'), stderr=subprocess.STDOUT)


if __name__ == '__main__':
    main()
