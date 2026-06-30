"""OKX 适配器：凭证(passphrase)/模拟盘头/资金费 8h/符号映射。"""
from gridtrade.exchanges.ccxt_adapter import CcxtAdapter


class OkxAdapter(CcxtAdapter):
    name = 'okx'
    FUNDING_INTERVAL_HOURS = 8

    def __init__(self, client):
        super().__init__(client, name='okx')

    # 规范 'BTC/USDT:USDT' <-> 原生 'BTC-USDT-SWAP'（结算币由 self.quote_currency 派生）
    def to_native(self, symbol: str) -> str:
        base = symbol.split('/')[0]
        return f'{base}-{self.quote_currency}-SWAP'

    def to_canonical(self, native: str) -> str:
        suffix = f'-{self.quote_currency}-SWAP'
        if native.endswith(suffix):
            q = self.quote_currency
            return f'{native[:-len(suffix)]}/{q}:{q}'
        return native

    @classmethod
    def from_credentials(cls, api_key, secret, password, *,
                         simulated=False, proxies=None, timeout=5000):
        import ccxt
        client = ccxt.okx({
            'apiKey': api_key, 'secret': secret, 'password': password,
            'timeout': timeout, 'enableRateLimit': True,
            'proxies': proxies or {},
        })
        if simulated:
            client.headers = dict(getattr(client, 'headers', None) or {},
                                  **{'x-simulated-trading': '1'})
        return cls(client)
