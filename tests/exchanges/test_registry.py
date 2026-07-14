import pytest


def test_build_fake():
    from gridtrade.exchanges.registry import build_adapter
    from gridtrade.exchanges.fake import FakeExchange
    assert isinstance(build_adapter({'exchange': 'fake'}), FakeExchange)


def test_build_binance():
    import ccxt
    from gridtrade.exchanges.registry import build_adapter
    from gridtrade.exchanges.binance import BinanceAdapter
    a = build_adapter({'exchange': 'binance', 'api_key': 'k', 'secret': 's'})
    assert isinstance(a, BinanceAdapter) and isinstance(a.client, ccxt.binanceusdm)
    assert a.quote_currency == 'USDT'


def test_build_binance_testnet():
    from gridtrade.exchanges.registry import build_adapter
    a = build_adapter({'exchange': 'binance', 'api_key': 'k', 'secret': 's',
                       'testnet': True})
    assert 'demo' in str(a.client.urls['api']).lower()   # testnet=True → 币安 Demo Trading


def test_quote_currency_override_applied():
    from gridtrade.exchanges.registry import build_adapter
    a = build_adapter({'exchange': 'binance', 'api_key': 'k', 'secret': 's',
                       'quote_currency': 'USDC'})
    assert a.quote_currency == 'USDC'    # USDC-M 之门保留（spec §3.2）


def test_removed_exchanges_raise():
    from gridtrade.exchanges.registry import build_adapter
    for name in ('hyperliquid', 'okx', 'nope'):
        with pytest.raises(ValueError):
            build_adapter({'exchange': name})
