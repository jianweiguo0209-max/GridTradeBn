def test_adapter_endpoint_testnet():
    from gridtrade.exchanges.resilient_adapter import ResilientAdapter
    from gridtrade.exchanges.binance import BinanceAdapter
    from gridtrade.runtime.introspect import adapter_endpoint
    ad = ResilientAdapter(BinanceAdapter.from_credentials('k', 's', testnet=True))
    assert 'testnet' in adapter_endpoint(ad)


def test_adapter_endpoint_mainnet():
    from gridtrade.exchanges.resilient_adapter import ResilientAdapter
    from gridtrade.exchanges.binance import BinanceAdapter
    from gridtrade.runtime.introspect import adapter_endpoint
    ad = ResilientAdapter(BinanceAdapter.from_credentials('k', 's'))
    ep = adapter_endpoint(ad)
    # 主网是 ccxt 模板 https://api.{hostname}（请求时替换）；关键是「不含 testnet」
    assert 'testnet' not in ep and 'api' in ep


def test_adapter_endpoint_fake_na():
    from gridtrade.exchanges.fake import FakeExchange
    from gridtrade.runtime.introspect import adapter_endpoint
    assert adapter_endpoint(FakeExchange()) == 'n/a'
