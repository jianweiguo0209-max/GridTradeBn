from tests.exchanges.test_ccxt_adapter import FakeCcxtClient


def _hl():
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
    return HyperliquidAdapter(FakeCcxtClient())


def test_symbol_mapping_roundtrip():
    a = _hl()
    # canonical 如实反映 HL 结算币 USDC（不再伪装成 USDT）
    assert a.to_canonical('BTC/USDC:USDC') == 'BTC/USDC:USDC'
    assert a.to_native('BTC/USDC:USDC') == 'BTC/USDC:USDC'
    assert a.to_native('ETH/USDC:USDC') == 'ETH/USDC:USDC'


def test_canonical_derives_from_quote_currency_override():
    # 实例覆写 quote_currency -> 符号随之派生（单一事实源）
    a = _hl()
    a.quote_currency = 'USDT'
    assert a.to_canonical('BTC/USDC:USDC') == 'BTC/USDT:USDT'
    assert a.to_native('BTC/USDT:USDT') == 'BTC/USDT:USDT'


def test_funding_interval_and_name():
    assert _hl().FUNDING_INTERVAL_HOURS == 1
    assert _hl().name == 'hyperliquid'


def test_from_credentials_builds_ccxt_client():
    import ccxt
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter
    a = HyperliquidAdapter.from_credentials('0xWALLET', '0xKEY')
    assert isinstance(a.client, ccxt.hyperliquid)


def test_list_instruments_excludes_builder_dex_assets():
    """builder-dex(HIP-3,如 hyna/xyz)资产从 universe 剔除:回测不可复现(Reservoir 归档无
    builder 数据)+ 部分 dex 用非 USDC 保证金(hyna=USDE)。判据=市场 info.dex 非空
    (主 dex 资产 dex=None;memory builder-dex-backtest-blindspot)。"""
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter

    class _C:
        def load_markets(self):
            return self.markets
        markets = {
            'BTC/USDC:USDC': {'swap': True, 'precision': {'price': 0.1, 'amount': 0.001},
                              'limits': {'amount': {'min': 0.001}}, 'active': True,
                              'info': {}},                                    # 主 dex → 留
            'HYNA-BTC/USDE:USDE': {'swap': True, 'precision': {}, 'limits': {},
                                   'active': True,
                                   'info': {'dex': 'hyna', 'hip3': True}},    # builder → 剔
            'XYZ-KIOXIA/USDC:USDC': {'swap': True, 'precision': {}, 'limits': {},
                                     'active': True,
                                     'info': {'dex': 'xyz'}},                 # builder(USDC 计价也剔)
        }

    a = HyperliquidAdapter(_C())
    syms = [i.symbol for i in a.list_instruments()]
    assert syms == ['BTC/USDC:USDC']
