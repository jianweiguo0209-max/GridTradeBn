import pytest


def test_build_fake():
    from gridtrade.exchanges.registry import build_adapter
    from gridtrade.exchanges.fake import FakeExchange
    a = build_adapter({'exchange': 'fake'})
    assert isinstance(a, FakeExchange)


def test_build_okx():
    import ccxt
    from gridtrade.exchanges.registry import build_adapter
    from gridtrade.exchanges.okx import OkxAdapter
    a = build_adapter({'exchange': 'okx', 'api_key': 'k', 'secret': 's',
                       'password': 'p', 'simulated': True})
    assert isinstance(a, OkxAdapter) and isinstance(a.client, ccxt.okx)


def test_build_hyperliquid():
    from gridtrade.exchanges.registry import build_adapter
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
    a = build_adapter({'exchange': 'hyperliquid', 'wallet_address': '0xW',
                       'private_key': '0xK'})
    assert isinstance(a, HyperliquidAdapter)


def test_unknown_raises():
    from gridtrade.exchanges.registry import build_adapter
    with pytest.raises(ValueError):
        build_adapter({'exchange': 'nope'})
