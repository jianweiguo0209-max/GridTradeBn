"""gridtrade.backtest 包。

导入即应用本地回测资源护栏：锁死每进程 BLAS 线程数，防「多进程 × OpenBLAS 满核线程」
把机器 thrash 到内核 panic。见 _resource_guard 与 docs/回测线程超订死机事故-2026-07-16.md。

_resource_guard 只依赖 os，先于任何 numpy/pandas 导入执行 → 线程锁及时生效。
"""
from gridtrade.backtest._resource_guard import apply_thread_caps, safe_workers

apply_thread_caps()

__all__ = ['apply_thread_caps', 'safe_workers']
