"""按配置构造交易所适配器（Factory）。"""
from gridtrade.exchanges.base import ExchangeAdapter
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
from gridtrade.exchanges.okx import OkxAdapter


def build_adapter(config: dict) -> ExchangeAdapter:
    name = (config.get('exchange') or '').lower()
    if name == 'fake':
        return FakeExchange()
    if name == 'okx':
        return OkxAdapter.from_credentials(
            config.get('api_key', ''), config.get('secret', ''),
            config.get('password', ''),
            simulated=bool(config.get('simulated', False)),
            proxies=config.get('proxies'))
    if name == 'hyperliquid':
        return HyperliquidAdapter.from_credentials(
            config.get('wallet_address', ''), config.get('private_key', ''),
            proxies=config.get('proxies'))
    raise ValueError(f'未知交易所: {name!r}（支持: okx/hyperliquid/fake）')
