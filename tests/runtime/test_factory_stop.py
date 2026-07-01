from gridtrade.config import load_deploy_config
from gridtrade.runtime.factory import build_runtime


def test_factory_threads_stop_config():
    cfg = load_deploy_config(env={
        'EXCHANGE': 'fake', 'STOP_ORDERS_ENABLED': 'true', 'STOP_SLIPPAGE': '0.2'})
    rt = build_runtime(cfg)
    assert rt.manager.executor.stop_orders_enabled is True
    assert rt.manager.executor.stop_slippage == 0.2
