from gridtrade.exchanges.ccxt_adapter import CcxtAdapter
from gridtrade.exchanges.hyperliquid import HyperliquidAdapter


class _Client:
    def __init__(self, bal):
        self._bal = bal
    def fetch_balance(self):
        return self._bal


def test_ccxt_default_quote_currency_is_usdt():
    ad = CcxtAdapter(_Client({'USDT': {'total': 100.0, 'free': 90.0, 'used': 10.0}}),
                     name='okx')
    b = ad.fetch_balance()
    assert b.equity == 100.0 and b.cash == 90.0


def test_hl_reads_usdc_balance():
    # HL 是 USDC 计价；读 USDT 会得 0
    ad = HyperliquidAdapter(_Client({
        'USDC': {'total': 500.0, 'free': 400.0, 'used': 100.0}, 'USDT': None}))
    b = ad.fetch_balance()
    assert b.equity == 500.0 and b.cash == 400.0
