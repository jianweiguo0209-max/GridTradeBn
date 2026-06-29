from gridtrade.state.control import ControlFlagRepository


def test_flag_defaults_false_then_set_toggle(store):
    flags = ControlFlagRepository(store)
    assert flags.get('trading_halted') is False        # 缺行默认 false
    flags.set('trading_halted', True, actor='admin')
    assert flags.get('trading_halted') is True
    flags.set('trading_halted', False, actor='admin')
    assert flags.get('trading_halted') is False        # upsert 覆盖
