from gridtrade.config import load_deploy_config
from gridtrade.runtime.factory import build_runtime


def _rt(**kw):
    env = {'EXCHANGE': 'fake'}
    env.update(kw)
    return build_runtime(load_deploy_config(env=env))


def test_run_monitor_once_restores_and_beats():
    from gridtrade.runtime.monitor import run_monitor
    rt = _rt()
    run_monitor(rt, once=True, sleep=lambda d: None)
    assert rt.heartbeats.get('monitor') is not None


def test_run_monitor_degrades_on_cycle_error_and_still_beats():
    from gridtrade.runtime.monitor import run_monitor
    rt = _rt()
    logs = []
    def _boom(reconciler, manager):
        raise RuntimeError('cycle blew up')
    # 单轮异常 -> 捕获 + log + 心跳 + 不抛出
    run_monitor(rt, once=True, sleep=lambda d: None, log=logs.append,
                cycle_fn=_boom)
    assert any('cycle blew up' in s or 'degraded' in s for s in logs)
    assert rt.heartbeats.get('monitor') is not None


def test_run_monitor_loops_until_should_stop():
    from gridtrade.runtime.monitor import run_monitor
    rt = _rt()
    n = {'i': 0}
    def _cycle(reconciler, manager):
        n['i'] += 1
        return {}
    # should_stop 第 3 轮后停
    run_monitor(rt, sleep=lambda d: None, cycle_fn=_cycle,
                should_stop=lambda: n['i'] >= 3)
    assert n['i'] == 3
