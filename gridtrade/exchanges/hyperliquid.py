"""Hyperliquid 适配器：钱包凭证/资金费 1h/USDC 计价符号映射。"""
from gridtrade.exchanges.ccxt_adapter import CcxtAdapter


class HyperliquidAdapter(CcxtAdapter):
    name = 'hyperliquid'
    FUNDING_INTERVAL_HOURS = 1

    def __init__(self, client):
        super().__init__(client, name='hyperliquid')

    # 规范 'BTC/USDT:USDT' <-> HL 原生 'BTC/USDC:USDC'
    def to_native(self, symbol: str) -> str:
        base = symbol.split('/')[0]
        return f'{base}/USDC:USDC'

    def to_canonical(self, native: str) -> str:
        base = native.split('/')[0]
        return f'{base}/USDT:USDT'

    def create_market_order(self, symbol, side, size, *,
                            reduce_only=False, client_oid=None):
        # HL 无真正市价单：ccxt 需一个参考价来算滑点上限（默认 5%）。传当前价。
        price = self.fetch_price(symbol)
        r = self.client.create_order(self.to_native(symbol), 'market', side, size,
                                     price, self._params(reduce_only, client_oid))
        return self._to_order(r)

    @classmethod
    def from_credentials(cls, wallet_address, private_key, *, proxies=None,
                         testnet=False):
        import ccxt
        client = ccxt.hyperliquid({
            'walletAddress': wallet_address,
            'privateKey': private_key,
            'enableRateLimit': True,
            'proxies': proxies or {},
        })
        if testnet:
            client.set_sandbox_mode(True)
        return cls(client)
