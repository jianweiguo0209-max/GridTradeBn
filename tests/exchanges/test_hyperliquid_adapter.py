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


def _mixed_markets():
    return {
        'BTC/USDC:USDC': {'swap': True, 'precision': {'price': 0.1, 'amount': 0.001},
                          'limits': {'amount': {'min': 0.001}}, 'active': True,
                          'settle': 'USDC', 'info': {}},
        'HYNA-BTC/USDE:USDE': {'swap': True, 'precision': {}, 'limits': {}, 'active': True,
                               'settle': 'USDE', 'info': {'dex': 'hyna', 'hip3': True}},
        'XYZ-TSLA/USDC:USDC': {'swap': True, 'precision': {}, 'limits': {}, 'active': True,
                               'settle': 'USDC', 'info': {'dex': 'xyz'}},
        'MKTS-US500/USDC:USDC': {'swap': True, 'precision': {}, 'limits': {}, 'active': True,
                                 'settle': 'USDC', 'info': {'dex': 'mkts'}},
    }


def test_builder_dex_whitelist_allows_usdc_dex_only():
    """spec 2026-07-12-builder-dex 阶段1：builder_dexes 白名单放行 USDC 结算的 builder 资产；
    ①默认空=整类剔除（现状零变化）②白名单内 USDC dex 放行③非 USDC dex 即使在白名单也剔
    ④白名单外 USDC dex 仍剔。"""
    from gridtrade.exchanges.hyperliquid import HyperliquidAdapter

    class _C:
        markets = _mixed_markets()
        def load_markets(self):
            return self.markets

    a = HyperliquidAdapter(_C())
    assert [i.symbol for i in a.list_instruments()] == ['BTC/USDC:USDC']   # ① 默认现状

    a.builder_dexes = ('xyz', 'hyna')
    syms = [i.symbol for i in a.list_instruments()]
    assert 'XYZ-TSLA/USDC:USDC' in syms                                    # ② USDC dex 放行
    assert 'HYNA-BTC/USDE:USDE' not in syms                                # ③ USDE 硬剔
    assert 'MKTS-US500/USDC:USDC' not in syms                              # ④ 白名单外仍剔
    assert 'BTC/USDC:USDC' in syms                                         # 主 dex 恒在
