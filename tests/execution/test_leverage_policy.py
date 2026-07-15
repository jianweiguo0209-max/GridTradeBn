"""开格设杠杆纯函数（spec 2026-07-15-open-set-leverage §3.2）。
用 demo 实测档位当夹具：KITE(5档 maxLev 5→1) / 1000PEPE(高杠杆多档)。"""
from gridtrade.execution.leverage_policy import cap_at_leverage, feasible, pick_leverage

GEARING = 3.4          # ceil = 4
KITE = [{'maxLeverage': 5, 'maxNotional': 5000.0}, {'maxLeverage': 4, 'maxNotional': 10000.0},
        {'maxLeverage': 3, 'maxNotional': 30000.0}, {'maxLeverage': 2, 'maxNotional': 80000.0},
        {'maxLeverage': 1, 'maxNotional': 200000.0}]
PEPE = [{'maxLeverage': 25, 'maxNotional': 5000.0}, {'maxLeverage': 20, 'maxNotional': 10000.0},
        {'maxLeverage': 13, 'maxNotional': 50000.0}, {'maxLeverage': 4, 'maxNotional': 1000000.0}]


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
