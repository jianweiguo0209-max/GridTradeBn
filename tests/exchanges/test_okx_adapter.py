from tests.exchanges.test_ccxt_adapter import FakeCcxtClient


def _okx():
    from gridtrade.exchanges.okx import OkxAdapter
    return OkxAdapter(FakeCcxtClient())


def test_symbol_mapping_roundtrip():
    a = _okx()
    assert a.to_native('BTC/USDT:USDT') == 'BTC-USDT-SWAP'
    assert a.to_canonical('BTC-USDT-SWAP') == 'BTC/USDT:USDT'
    assert a.to_native('ETH/USDT:USDT') == 'ETH-USDT-SWAP'


def test_funding_interval():
    assert _okx().FUNDING_INTERVAL_HOURS == 8
    assert _okx().name == 'okx'


def test_simulated_header_applied():
    import ccxt
    from gridtrade.exchanges.okx import OkxAdapter
    a = OkxAdapter.from_credentials('k', 's', 'p', simulated=True)
    assert isinstance(a.client, ccxt.okx)
    assert a.client.headers.get('x-simulated-trading') == '1'
