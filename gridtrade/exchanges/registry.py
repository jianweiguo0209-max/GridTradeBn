"""按配置构造交易所适配器（Factory）。"""
from gridtrade.exchanges.base import ExchangeAdapter
from gridtrade.exchanges.binance import BinanceAdapter
from gridtrade.exchanges.fake import FakeExchange


def build_adapter(config: dict) -> ExchangeAdapter:
    name = (config.get('exchange') or '').lower()
    if name == 'fake':
        adapter = FakeExchange()
    elif name == 'binance':
        adapter = BinanceAdapter.from_credentials(
            config.get('api_key', ''), config.get('secret', ''),
            testnet=bool(config.get('testnet', False)),
            proxies=config.get('proxies'))
    else:
        raise ValueError(f'未知交易所: {name!r}（支持: binance/fake）')
    # 可选覆写计价/结算币：非空才覆写（同所多结算之门：USDT-M 默认 / USDC-M 显式设）。
    # 实例属性同时驱动符号拼接与读余额（单一事实源）。
    qc = config.get('quote_currency')
    if qc:
        adapter.quote_currency = qc
    return adapter
