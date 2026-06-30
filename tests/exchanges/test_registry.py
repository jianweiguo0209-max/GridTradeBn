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


def test_quote_currency_override_applied():
    # config 带 quote_currency -> 覆写适配器实例（符号 + 余额单一事实源）
    from gridtrade.exchanges.registry import build_adapter
    a = build_adapter({'exchange': 'hyperliquid', 'wallet_address': '0xW',
                       'private_key': '0xK', 'quote_currency': 'USDT'})
    assert a.quote_currency == 'USDT'
    assert a.to_canonical('BTC/USDT:USDT') == 'BTC/USDT:USDT'


def test_quote_currency_absent_uses_class_default():
    # 不带 quote_currency -> 用类默认（HL=USDC）
    from gridtrade.exchanges.registry import build_adapter
    a = build_adapter({'exchange': 'hyperliquid', 'wallet_address': '0xW',
                       'private_key': '0xK'})
    assert a.quote_currency == 'USDC'


def test_unknown_raises():
    from gridtrade.exchanges.registry import build_adapter
    with pytest.raises(ValueError):
        build_adapter({'exchange': 'nope'})
