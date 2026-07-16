"""本地多进程回测资源护栏（2026-07-16 事故后加）。

事故：numpy 用 OpenBLAS，每进程默认开 =核数 条 BLAS 线程；回测 `workers=8` 起 8 个
worker 进程 → 8×8 = 最多 64 条线程抢 8 核 → load average 飙 29 → 调度 thrash 饿死
watchdogd → 内核 watchdog-timeout panic 重启（实证两次）或过载假死（第三次）。
详见 docs/回测线程超订死机事故-2026-07-16.md。

本包不被实盘代码 import（已核 gridtrade.runtime/execution/exchanges/core 无引用），
故护栏只影响离线回测，不碰实盘性能。
"""
import os

# BLAS/OpenMP 每进程线程数环境变量。OpenBLAS/OpenMP 在库首次 dlopen（即 numpy import）时
# 读取，之后改无效——故必须在任何 import numpy/pandas 之前 setdefault。
_THREAD_VARS = ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS')


def apply_thread_caps():
    """把每进程 BLAS/OpenMP 线程锁到 1，杜绝 workers × 满核线程 的超订爆炸。

    - 必须在 numpy 首次 import 前调用（本包 __init__ 顶部已调，先于所有子模块）。
    - setdefault：保留用户显式 export 的值（如需放开可自行 export）。
    - ProcessPoolExecutor 的 worker 子进程经 os.environ 继承此设置 → 子进程 numpy
      载入时即 1 线程，故父进程在 spawn 前设好即可保护真正吃 CPU 的 worker。
    """
    for v in _THREAD_VARS:
        os.environ.setdefault(v, '1')


def safe_workers(requested):
    """把回测 worker 进程数夹到 ≤ 半数核心，防 CPU 超订把机器 thrash 到假死/内核 panic。

    只降不升；被夹时打印告警（不静默）。半数核心给系统留足调度余量：即便每 worker
    已锁 1 BLAS 线程，跑满全部核心也会让整机 beachball，留一半才安全可用。
    """
    ncpu = os.cpu_count() or 4
    cap = max(1, ncpu // 2)
    n = max(1, min(int(requested), cap))
    if int(requested) > cap:
        print('[backtest] ⚠️ workers=%d 超安全上限(半数核心=%d)，已夹到 %d 防机器假死'
              % (int(requested), cap, n), flush=True)
    return n
