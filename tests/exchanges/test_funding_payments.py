from gridtrade.exchanges.base import Instrument


def _fake():
    from gridtrade.exchanges.fake import FakeExchange
    return FakeExchange(instruments=[Instrument('BTC/USDT:USDT', 0.1, 0.001, 0.001, 'live', 0)])


def test_fake_seed_and_fetch_funding_payments():
    from gridtrade.exchanges.base import FundingPayment
    ex = _fake()
    ex.seed_funding_payments('BTC/USDT:USDT', [(1000, 0.5), (2000, -0.3), (3000, 0.2)])
    out = ex.fetch_funding_payments('BTC/USDT:USDT')
    assert all(isinstance(p, FundingPayment) for p in out)
    assert [(p.ts, p.amount) for p in out] == [(1000, 0.5), (2000, -0.3), (3000, 0.2)]


def test_fake_funding_payments_since_filter():
    ex = _fake()
    ex.seed_funding_payments('BTC/USDT:USDT', [(1000, 0.5), (2000, -0.3), (3000, 0.2)])
    out = ex.fetch_funding_payments('BTC/USDT:USDT', since_ms=2000)
    assert [(p.ts, p.amount) for p in out] == [(2000, -0.3), (3000, 0.2)]


def test_ccxt_funding_payments_sign_and_mapping():
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    from gridtrade.exchanges.base import FundingPayment

    class FakeClient:
        def fetch_funding_history(self, symbol, since=None, limit=None, params=None):
            # ccxt 约定：amount 负=支付，正=收取
            return [{'timestamp': 1000, 'amount': -0.5, 'symbol': symbol},
                    {'timestamp': 2000, 'amount': 0.3, 'symbol': symbol}]

    a = CcxtAdapter(FakeClient(), name='ccxt')
    out = a.fetch_funding_payments('BTC/USDT:USDT')
    assert out == [FundingPayment(ts=1000, amount=0.5), FundingPayment(ts=2000, amount=-0.3)]


def test_ccxt_funding_payments_filters_to_requested_symbol():
    # 真实 HL：fetch_funding_history(symbol) 忽略过滤、返回账户级全币种流水（各行自带 symbol）。
    # 适配器必须只保留本币种，否则会把别的币种 funding 计入本网格。
    from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
    from gridtrade.exchanges.base import FundingPayment

    class AccountWideClient:
        def fetch_funding_history(self, symbol, since=None, limit=None, params=None):
            return [{'timestamp': 1000, 'amount': -0.5, 'symbol': 'BTC/USDT:USDT'},
                    {'timestamp': 1500, 'amount': -0.9, 'symbol': 'ETH/USDT:USDT'},
                    {'timestamp': 2000, 'amount': 0.3, 'symbol': 'BTC/USDT:USDT'}]

    a = CcxtAdapter(AccountWideClient(), name='ccxt')
    out = a.fetch_funding_payments('BTC/USDT:USDT')
    assert out == [FundingPayment(ts=1000, amount=0.5), FundingPayment(ts=2000, amount=-0.3)]


def test_hyperliquid_funding_payments_filter_uses_native_symbol():
    # HL 规范符号 BTC/USDT:USDT <-> 原生 BTC/USDC:USDC；过滤须按 native 符号匹配。
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
    from gridtrade.exchanges.base import FundingPayment

    class AccountWideClient:
        def fetch_funding_history(self, symbol, since=None, limit=None, params=None):
            return [{'timestamp': 1000, 'amount': -0.5, 'symbol': 'BTC/USDC:USDC'},
                    {'timestamp': 1500, 'amount': -0.9, 'symbol': 'ETH/USDC:USDC'}]

    a = HyperliquidAdapter(AccountWideClient())
    out = a.fetch_funding_payments('BTC/USDT:USDT')
    assert out == [FundingPayment(ts=1000, amount=0.5)]


def test_adapter_declares_fetch_funding_payments_abstract():
    from gridtrade.exchanges.base import ExchangeAdapter
    assert 'fetch_funding_payments' in ExchangeAdapter.__abstractmethods__
