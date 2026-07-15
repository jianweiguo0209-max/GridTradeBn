"""保险丝覆盖率策略纯函数（spec 2026-07-15 §四/§六）。
worst = 每笔数量 × grid_count（与 executor.open 同源：max_rate=1.0）；
coverage = maxQty/worst；不足额时降 cap 到刚好足额（coverage'=1.0）。"""
import pytest

from gridtrade.execution.fuse_policy import (audit_fuse_coverage, fuse_capped_cap,
                                             fuse_worst)

GEARING = 3.4
GP = {'low_price': 100.0, 'high_price': 120.0, 'grid_count': 20,
      'stop_low_price': 95.0, 'stop_high_price': 125.0}


def test_worst_matches_executor_formula():
    # 与 executor.open 同源：grid_order_info(max_rate=1.0) 的每笔数量 × grid_count
    from gridtrade.core.grid_engine import grid_order_info
    gi = grid_order_info(100.0, GEARING, GP['low_price'], GP['high_price'],
                         GP['grid_count'], GP['stop_low_price'], GP['stop_high_price'],
                         min_amount=0.0, max_rate=1.0)
    assert fuse_worst(100.0, GEARING, GP) == pytest.approx(
        float(gi['每笔数量']) * GP['grid_count'])


def test_full_coverage_leaves_cap_untouched():
    # maxQty 远大于 worst → 足额，cap 原样，coverage>1
    w = fuse_worst(100.0, GEARING, GP)
    cap2, cov = fuse_capped_cap(100.0, GEARING, GP, w * 10)
    assert cap2 == 100.0 and cov == pytest.approx(10.0)


def test_shortfall_caps_down_to_exactly_full():
    # maxQty = worst 的一半 → 降 cap 到足额（无取整时 worst' 恰 == maxQty，不多缩一分仓位）
    w = fuse_worst(100.0, GEARING, GP)
    mx = w / 2.0
    cap2, cov = fuse_capped_cap(100.0, GEARING, GP, mx)
    assert cov == pytest.approx(0.5)              # 干预前的覆盖率
    assert cap2 == pytest.approx(50.0)            # 线性缩放
    w2 = fuse_worst(cap2, GEARING, GP)
    assert w2 <= mx * (1 + 1e-9)                  # 足额（护全额）
    assert w2 == pytest.approx(mx)                # 且刚好——不多缩一分仓位


def test_capdown_never_raises_on_lot_step_boundary():
    # 取整阶梯回归（评审实证 2026-07-15）：覆盖率 99% + min_amount=0.001 时，
    # 旧算法 cap×coverage 会让每笔数量落同一档不变 → worst' 不降 → 断言抛异常。
    # 新算法（未取整 worst 求解）必须既不抛异常、又真的足额。
    gp = dict(GP)
    w = fuse_worst(10.0, 1.0, gp, min_amount=0.001)
    mx = w * 0.99                                  # 最常见的"差一点"场景
    cap2, cov = fuse_capped_cap(10.0, 1.0, gp, mx, min_amount=0.001)   # 不得抛
    assert cov == pytest.approx(0.99)
    w2 = fuse_worst(cap2, 1.0, gp, min_amount=0.001)
    assert w2 is not None and w2 <= mx * (1 + 1e-9)     # 取整后仍足额


def test_capdown_never_increases_cap():
    # 护栏绝不放大仓位（评审实证 2026-07-15）：min_coverage>1 时已足额币（coverage∈[1,mc)）
    # 也会进干预分支——必须 clamp 成不动，否则 cap 会被放大到 worst==maxQty。
    w = fuse_worst(100.0, GEARING, GP)
    for mc, mx_mult in ((1.2, 1.10), (2.0, 1.90)):      # 已足额（coverage>1）却低于 mc
        cap2, cov = fuse_capped_cap(100.0, GEARING, GP, w * mx_mult, min_coverage=mc)
        assert cov == pytest.approx(mx_mult)
        assert cap2 == pytest.approx(100.0)             # 只降不升（此处应恰为不动，非仅 <=）


def test_unknown_max_qty_fails_open():
    # maxQty 未知（0/None）→ 不干预（交易所自会校验）
    for mx in (0.0, None):
        cap2, cov = fuse_capped_cap(100.0, GEARING, GP, mx)
        assert cap2 == 100.0 and cov is None


def test_min_coverage_zero_disables_intervention_but_still_reports():
    # 停用开关：只算 coverage 供审计，不降 cap
    w = fuse_worst(100.0, GEARING, GP)
    cap2, cov = fuse_capped_cap(100.0, GEARING, GP, w / 2.0, min_coverage=0.0)
    assert cap2 == 100.0 and cov == pytest.approx(0.5)


def test_min_coverage_is_trigger_threshold_not_target():
    # min_coverage 只是触发阈值：0.8 容忍 0.9（不动），但 0.5 触发后降到足额（非 0.8）
    w = fuse_worst(100.0, GEARING, GP)
    cap_a, _ = fuse_capped_cap(100.0, GEARING, GP, w * 0.9, min_coverage=0.8)
    assert cap_a == 100.0                                   # 0.9 ≥ 0.8 → 容忍
    cap_b, _ = fuse_capped_cap(100.0, GEARING, GP, w * 0.5, min_coverage=0.8)
    assert fuse_worst(cap_b, GEARING, GP) == pytest.approx(w * 0.5)   # 降到足额，非 0.8


def test_min_amount_rounding_still_within_max_qty():
    # min_amount 向下取整只减不增 → 降档后仍达标（不得因取整反超 maxQty）
    w = fuse_worst(100.0, GEARING, GP, min_amount=0.001)
    mx = w * 0.37
    cap2, _ = fuse_capped_cap(100.0, GEARING, GP, mx, min_amount=0.001)
    w2 = fuse_worst(cap2, GEARING, GP, min_amount=0.001)
    assert w2 is not None and w2 <= mx * (1 + 1e-9)


def test_ungriddable_cap_fails_open():
    # cap 太低 → grid_order_info 返 None → 不干预（交给 MinNotionalGate 拒）
    assert fuse_worst(0.0, GEARING, GP) is None
    cap2, cov = fuse_capped_cap(0.0, GEARING, GP, 1.0)
    assert cap2 == 0.0 and cov is None


def test_audit_lists_shortfall_sorted_and_skips_unknown():
    # 票池审计（近似口径）：满仓名义 = cap×gearing；足额 ⟺ maxQty×price ≥ 满仓名义
    au = audit_fuse_coverage(
        ['A/USDT:USDT', 'B/USDT:USDT', 'C/USDT:USDT', 'D/USDT:USDT'],
        prices={'A/USDT:USDT': 1.0, 'B/USDT:USDT': 1.0, 'C/USDT:USDT': 1.0},
        max_qtys={'A/USDT:USDT': 100.0, 'B/USDT:USDT': 50.0, 'C/USDT:USDT': 340.0,
                  'D/USDT:USDT': 999.0},
        cap=100.0, gearing=GEARING)                       # 满仓名义 = 340
    assert au['need'] == pytest.approx(340.0)
    assert au['total'] == 3                               # D 缺价 → 跳过（不参与审计）
    assert [s for s, _ in au['short']] == ['B/USDT:USDT', 'A/USDT:USDT']  # 覆盖率升序
    assert au['short'][0][1] == pytest.approx(50.0 / 340.0)


def test_audit_all_covered():
    au = audit_fuse_coverage(['A/USDT:USDT'], prices={'A/USDT:USDT': 1.0},
                             max_qtys={'A/USDT:USDT': 10_000.0},
                             cap=100.0, gearing=GEARING)
    assert au['short'] == [] and au['total'] == 1
