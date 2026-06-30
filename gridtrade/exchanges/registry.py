"""按配置构造交易所适配器（Factory）。"""
from gridtrade.exchanges.base import ExchangeAdapter
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
from gridtrade.exchanges.okx import OkxAdapter


def build_adapter(config: dict) -> ExchangeAdapter:
    name = (config.get('exchange') or '').lower()
    if name == 'fake':
        adapter = FakeExchange()
    elif name == 'okx':
        adapter = OkxAdapter.from_credentials(
            config.get('api_key', ''), config.get('secret', ''),
            config.get('password', ''),
            simulated=bool(config.get('simulated', False)),
            proxies=config.get('proxies'))
    elif name == 'hyperliquid':
        adapter = HyperliquidAdapter.from_credentials(
            config.get('wallet_address', ''), config.get('private_key', ''),
            proxies=config.get('proxies'),
            testnet=bool(config.get('testnet', False)))
    else:
        raise ValueError(f'未知交易所: {name!r}（支持: okx/hyperliquid/fake）')
    # 可选覆写计价/结算币：非空才覆写，否则保留适配器类默认（HL=USDC / OKX=USDT）。
    # 实例属性同时驱动符号拼接与读余额（单一事实源）。
    qc = config.get('quote_currency')
    if qc:
        adapter.quote_currency = qc
    return adapter
