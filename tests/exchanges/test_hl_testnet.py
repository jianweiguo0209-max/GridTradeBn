from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
from gridtrade.exchanges.registry import build_adapter


def test_testnet_uses_sandbox_url():
    ad = HyperliquidAdapter.from_credentials('0xabc', 'key', testnet=True)
    assert 'testnet' in ad.client.urls['api']['public']


def test_mainnet_default_no_sandbox():
    ad = HyperliquidAdapter.from_credentials('0xabc', 'key')
    assert 'testnet' not in ad.client.urls['api']['public']


def test_build_adapter_passes_testnet():
    ad = build_adapter({'exchange': 'hyperliquid', 'wallet_address': '0xabc',
                        'private_key': 'key', 'testnet': True})
    assert 'testnet' in ad.client.urls['api']['public']
