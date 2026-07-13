from gridtrade.exchanges.ccxt_adapter import CcxtAdapter


class _Client:
    def __init__(self, bal):
        self._bal = bal
    def fetch_balance(self):
        return self._bal


def test_ccxt_default_quote_currency_is_usdt():
    ad = CcxtAdapter(_Client({'USDT': {'total': 100.0, 'free': 90.0, 'used': 10.0}}),
                     name='ccxt')
    b = ad.fetch_balance()
    assert b.equity == 100.0 and b.cash == 90.0


def test_adapter_reads_balance_by_its_own_quote_currency():
    # 子类可覆写 quote_currency（结算币非 USDT 的交易所）；fetch_balance 必须按
    # self.quote_currency 取键，而非写死 USDT，否则非 USDT 结算的交易所永远读到 0。
    class UsdcQuoted(CcxtAdapter):
        quote_currency = 'USDC'

    ad = UsdcQuoted(_Client({
        'USDC': {'total': 500.0, 'free': 400.0, 'used': 100.0}, 'USDT': None}))
    b = ad.fetch_balance()
    assert b.equity == 500.0 and b.cash == 400.0
