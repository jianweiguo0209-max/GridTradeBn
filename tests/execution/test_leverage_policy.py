"""开格设杠杆纯函数（spec 2026-07-15-open-set-leverage §3.2）。
用 demo 实测档位当夹具：KITE(5档 maxLev 5→1) / 1000PEPE(高杠杆多档)。"""
from gridtrade.execution.leverage_policy import cap_at_leverage

GEARING = 3.4          # ceil = 4
KITE = [{'maxLeverage': 5, 'maxNotional': 5000.0}, {'maxLeverage': 4, 'maxNotional': 10000.0},
        {'maxLeverage': 3, 'maxNotional': 30000.0}, {'maxLeverage': 2, 'maxNotional': 80000.0},
        {'maxLeverage': 1, 'maxNotional': 200000.0}]
PEPE = [{'maxLeverage': 25, 'maxNotional': 5000.0}, {'maxLeverage': 20, 'maxNotional': 10000.0},
        {'maxLeverage': 13, 'maxNotional': 50000.0}, {'maxLeverage': 4, 'maxNotional': 1000000.0}]
# 第一档=10x:$2555 落第一档,pick_leverage 减一档=8<10（旧口径误剔）,第一档 10≥10（新口径保留）
TEN = [{'maxLeverage': 10, 'maxNotional': 5000.0}, {'maxLeverage': 8, 'maxNotional': 50000.0},
       {'maxLeverage': 5, 'maxNotional': 500000.0}]
# 第一档=20x 但 $2555 减一档=8<10（旧口径也误剔——第一档 20x 的正常币!）
TWENTY = [{'maxLeverage': 20, 'maxNotional': 5000.0}, {'maxLeverage': 8, 'maxNotional': 50000.0},
          {'maxLeverage': 4, 'maxNotional': 500000.0}]


def test_cap_at_leverage():
    assert cap_at_leverage(KITE, 4) == 10000.0      # maxLev>=4 的最大 maxNotional = 4x 档 $10k
    assert cap_at_leverage(KITE, 5) == 5000.0
    assert cap_at_leverage(KITE, 1) == 200000.0
    assert cap_at_leverage(KITE, 99) == 0.0         # 无 maxLev>=99 档









def test_worst_side_notional_max_of_sides():
    # 单侧最坏名义 = max(Σ买侧, Σ卖侧)(币安 IM 轨迹恒等式,spec 2026-07-19 地基②)
    from gridtrade.execution.leverage_policy import worst_side_notional
    assert worst_side_notional([100.0, 200.0, 400.0], 1.0, 150.0) == 600.0   # 买100 卖600
    assert worst_side_notional([100.0, 200.0, 400.0], 1.0, 450.0) == 700.0   # 全买侧
    assert worst_side_notional([100.0, 200.0, 400.0], 2.0, 150.0) == 1200.0  # qty 线性


def test_pick_leverage_max_highest_covering_bracket():
    # 能容 need 的最高档,不减档(余量由调用方 ×BRACKET_HEADROOM 显式化;
    # 全仓 L 不影响强平——spec 2026-07-19 地基①③)
    from gridtrade.execution.leverage_policy import pick_leverage_max
    RAVE = [{'maxLeverage': 20, 'maxNotional': 5000.0}, {'maxLeverage': 10, 'maxNotional': 10000.0},
            {'maxLeverage': 5, 'maxNotional': 50000.0}, {'maxLeverage': 1, 'maxNotional': 5000000.0}]
    assert pick_leverage_max(1533.0, RAVE) == 20      # 单侧×1.2 落首档 → 最高档(旧机制给 10)
    assert pick_leverage_max(5000.0, RAVE) == 20      # 边界:=maxNotional 含
    assert pick_leverage_max(6000.0, RAVE) == 10      # 超首档 → 自动降档
    assert pick_leverage_max(60000.0, RAVE) == 1      # 5万档也超 → 500万档
    assert pick_leverage_max(9e9, RAVE) == 1          # 全不容 → 最低档尽力(调用方告警)
    assert pick_leverage_max(1533.0, []) is None      # tiers 空 → fail-open 不设杠杆


def test_open_order_im_netting_rule_measured_states():
    # 币安 openOrderIM 净额规则(4 状态 demo 实测逆向,spec 地基②):
    # 多仓: max(Σ买, max(0, Σ卖 − 2×仓));2026-07-18 合成态分毫回归
    from gridtrade.execution.leverage_policy import open_order_im_notional
    assert abs(open_order_im_notional(0.0, 381.45, 120.71) - 140.03) < 0.01   # 合成态 $14.00×10
    assert open_order_im_notional(0.0, 253.0, 134.0) == 0.0                   # 扫到底:卖<2×仓 → 0
    assert open_order_im_notional(64.58, 191.6, 66.92) == 64.58               # 扫一半:买侧主导
    assert open_order_im_notional(253.0, 0.0, -134.0) == 0.0                  # 空仓对称


def test_total_im_invariant_equals_worst_side():
    # 恒等式:网格任意轨迹状态(仓位 p、剩买 B−p、卖 S+p)总 IM ≡ max(B,S)
    # (三态 demo 实测 $13.15/$13.48 ≈ 单侧 $13.38;代数对所有 p 成立)
    from gridtrade.execution.leverage_policy import open_order_im_notional
    B, S = 600.0, 500.0
    for p in (0.0, 100.0, 300.0, 600.0):
        total = p + open_order_im_notional(B - p, S + p, p)
        assert abs(total - max(B, S)) < 1e-9, (p, total)


def test_normalize_tiers_map_strips_and_coerces():
    # ccxt bulk 原始映射 → 标准 {sym: [{'maxLeverage','maxNotional'}]}:去空档/缺键容错,
    # 实盘缓存(binance.fetch_max_leverages)与回测(exclude_low_leverage)共用(单一事实源)
    from gridtrade.execution.leverage_policy import normalize_tiers_map
    raw = {'A/USDT:USDT': [{'maxLeverage': '20', 'maxNotional': '10000', 'tier': 1},
                           {'maxNotional': 99.0}],          # 缺 maxLeverage 的档丢弃
           'B/USDT:USDT': [],                               # 空档位 → 整币丢弃
           'C/USDT:USDT': None}
    out = normalize_tiers_map(raw)
    assert out == {'A/USDT:USDT': [{'maxLeverage': 20, 'maxNotional': 10000.0}]}
    assert normalize_tiers_map(None) == {}


def test_eligible_min_leverage_filters_low_bracket_coins():
    # 票池杠杆预过滤：判据=**第一档最大杠杆**(币安风险分级),第一档<min_lev 才剔。
    from gridtrade.execution.leverage_policy import eligible_min_leverage
    tmap = {'PEPE/USDT:USDT': PEPE, 'KITE/USDT:USDT': KITE}
    kept, dropped = eligible_min_leverage(['PEPE/USDT:USDT', 'KITE/USDT:USDT'],
                                          tmap, 2555.0, GEARING, 10.0)
    assert kept == ['PEPE/USDT:USDT']       # 第一档 25x ≥ 10
    assert dropped == ['KITE/USDT:USDT']    # 第一档 5x < 10


def test_eligible_min_leverage_keeps_first_tier_ge_min_not_pickL():
    """判据=第一档最大杠杆,非 pick_leverage($notional) 的「减一档」值(2026-07-19 修正)。
    第一档=10x/20x 的正常币,pick_L 减一档后 <10,此前被误剔(实测 137 个 10x + 8 个 20x)。
    「只过滤小于10倍」= 第一档 <10 才剔,10x/20x 全留。"""
    from gridtrade.execution.leverage_policy import eligible_min_leverage
    tmap = {'TEN/USDT:USDT': TEN, 'TWENTY/USDT:USDT': TWENTY, 'KITE/USDT:USDT': KITE}
    kept, dropped = eligible_min_leverage(
        ['TEN/USDT:USDT', 'TWENTY/USDT:USDT', 'KITE/USDT:USDT'], tmap, 2555.0, GEARING, 10.0)
    assert 'TEN/USDT:USDT' in kept and 'TWENTY/USDT:USDT' in kept    # 第一档 10x/20x ≥ 10 → 留
    assert dropped == ['KITE/USDT:USDT']                            # 第一档 5x < 10 → 剔
    # (旧「减一档」pick_leverage 已随币安原生机制删除,spec 2026-07-19;
    # 开仓选档现走 pick_leverage_max:TEN@单侧1533→10x、TWENTY→20x,均 ≥10,池门与开仓一致)


def test_eligible_min_leverage_boundary_equals_min_lev_kept():
    """第一档正好=min_lev 保留(「只过滤小于10倍」=严格 <,10 保留)。"""
    from gridtrade.execution.leverage_policy import eligible_min_leverage
    kept, dropped = eligible_min_leverage(['TEN/USDT:USDT'], {'TEN/USDT:USDT': TEN},
                                          2555.0, GEARING, 10.0)
    assert kept == ['TEN/USDT:USDT'] and dropped == []


def test_eligible_min_leverage_missing_tiers_fail_open():
    from gridtrade.execution.leverage_policy import eligible_min_leverage
    kept, dropped = eligible_min_leverage(['X/USDT:USDT'], {}, 2555.0, GEARING, 10.0)
    assert kept == ['X/USDT:USDT'] and dropped == []


def test_eligible_min_leverage_disabled_when_zero():
    from gridtrade.execution.leverage_policy import eligible_min_leverage
    tmap = {'KITE/USDT:USDT': KITE}
    kept, dropped = eligible_min_leverage(['KITE/USDT:USDT'], tmap, 2555.0, GEARING, 0.0)
    assert kept == ['KITE/USDT:USDT'] and dropped == []
