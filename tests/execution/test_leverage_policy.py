"""开格设杠杆纯函数（spec 2026-07-15-open-set-leverage §3.2）。
用 demo 实测档位当夹具：KITE(5档 maxLev 5→1) / 1000PEPE(高杠杆多档)。"""
from gridtrade.execution.leverage_policy import cap_at_leverage, feasible, pick_leverage

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


def test_feasible():
    assert feasible(8000.0, KITE, GEARING) is True     # $8k <= cap_at(4)=$10k → 可行
    assert feasible(12000.0, KITE, GEARING) is False   # $12k > $10k → 不可行(需 3x<gearing)
    assert feasible(999999.0, [], GEARING) is True     # tiers 空 → fail-open 判可行(不告警)


def test_pick_leverage_steps_down_one_bracket():
    # 1000PEPE worst $2000：tightest=25x($5k,idx0) → 减一档=20x
    assert pick_leverage(2000.0, PEPE, GEARING) == 20


def test_pick_leverage_floor_clamps_to_ceil_gearing():
    # KITE worst $8000：tightest=4x($10k,idx1) → 减一档=3x → floor clamp 到 ceil(3.4)=4
    assert pick_leverage(8000.0, KITE, GEARING) == 4


def test_pick_leverage_infeasible_best_effort():
    # KITE worst $12000（不可行）：超 4x 档 → 减一档到 2x → floor clamp 到 4（尽力；feasible 会告警）
    assert pick_leverage(12000.0, KITE, GEARING) == 4


def test_pick_leverage_worst_exceeds_all_brackets():
    # worst 超最大档($200k) → 最低档 1x 尽力 → floor clamp 到 4
    assert pick_leverage(500000.0, KITE, GEARING) == 4


def test_pick_leverage_empty_tiers_returns_none():
    assert pick_leverage(2000.0, [], GEARING) is None    # fail-open：调用方不设杠杆


def test_pick_leverage_never_exceeds_symbol_max():
    # worst 极小落 bracket0：减一档=20x，但绝不超最高档 25x
    assert pick_leverage(1.0, PEPE, GEARING) == 20


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
    from gridtrade.execution.leverage_policy import eligible_min_leverage, pick_leverage
    tmap = {'TEN/USDT:USDT': TEN, 'TWENTY/USDT:USDT': TWENTY, 'KITE/USDT:USDT': KITE}
    kept, dropped = eligible_min_leverage(
        ['TEN/USDT:USDT', 'TWENTY/USDT:USDT', 'KITE/USDT:USDT'], tmap, 2555.0, GEARING, 10.0)
    assert 'TEN/USDT:USDT' in kept and 'TWENTY/USDT:USDT' in kept    # 第一档 10x/20x ≥ 10 → 留
    assert dropped == ['KITE/USDT:USDT']                            # 第一档 5x < 10 → 剔
    # 佐证「减一档」bug:这俩的 pick_L 确实 <10（旧口径会误剔）,但开仓仍走 pick_leverage,不受本改动影响
    assert pick_leverage(2555.0, TEN, GEARING) < 10
    assert pick_leverage(2555.0, TWENTY, GEARING) < 10


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
