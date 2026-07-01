from gridtrade.config import load_deploy_config


def test_stop_defaults():
    c = load_deploy_config(env={})
    assert c.stop_orders_enabled is True
    assert c.stop_slippage == 0.15


def test_stop_env_override():
    c = load_deploy_config(env={'STOP_ORDERS_ENABLED': 'false',
                                'STOP_SLIPPAGE': '0.2'})
    assert c.stop_orders_enabled is False
    assert c.stop_slippage == 0.2
