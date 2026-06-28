"""monitor 机入口（常驻）：启动自愈 restore_all，循环 run_monitor_cycle + 心跳。

单轮异常降级 log+续跑（绝不 sys.exit）；SIGTERM/SIGINT 优雅停（完成当前轮后退出）。
"""
import signal
import time

from gridtrade.config import load_deploy_config
from gridtrade.runtime.cycles import restore_all, run_monitor_cycle
from gridtrade.runtime.factory import build_runtime
from gridtrade.runtime.introspect import adapter_endpoint


def run_monitor(runtime, *, once=False, sleep=time.sleep, log=print,
                cycle_fn=run_monitor_cycle, should_stop=None):
    rt = runtime
    restore_all(rt.reconciler)            # 重启自愈一次
    while True:
        try:
            cycle_fn(rt.reconciler, rt.manager)
        except Exception as exc:          # 降级：记录 + 续跑，绝不退出
            log('[monitor] degraded: %r' % exc)
        rt.heartbeats.beat('monitor')
        if once:
            return
        if should_stop is not None and should_stop():
            return
        sleep(rt.config.monitor_interval_sec)


def main() -> None:   # composition root（不单测）
    rt = build_runtime(load_deploy_config())
    print('[monitor] exchange=%s testnet=%s endpoint=%s interval=%ss'
          % (rt.config.exchange, rt.config.testnet, adapter_endpoint(rt.adapter),
             rt.config.monitor_interval_sec), flush=True)
    stop = {'flag': False}

    def _graceful(signum, frame):
        stop['flag'] = True

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    run_monitor(rt, should_stop=lambda: stop['flag'])


if __name__ == '__main__':
    main()
