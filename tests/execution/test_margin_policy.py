"""margin_policy.ladder_margin_required 纯函数（spec 2026-07-18-margin-gate-exchange-im）。

口径：required = k × (整梯双侧名义/L + worst止损浮亏 + 手续费ε)；L 与 executor.open 的
pick_leverage 同源（worst_exec = order_num × grid_count × entry）。返回 None = 无法计算
（tiers 空 / 建网 None / L None），调用方 fail-closed 回退 cap 口径。

手算基准（干净数）：low=100 high=400 count=2 → q=2、档 [100,200,400]、Σ=700；
cap=70 gearing=10 → order_num=70×10/700=1.0；entry=150 → 买{100} 卖{200,400}；
tiers [10x@1000, 5x@5000] → worst_exec=1×2×150=300 → 最紧档 10x@1000、减一档 5x、
clamp floor=ceil(10)=10 → L=10；IM=700/10=70；loss_down=1×(100−50)=50、
loss_up=(500−200)+(500−400)=400 → worst=400；fee(0.001)=0.7；
required = k×470.7。
"""
import pytest

from gridtrade.execution.margin_policy import ladder_margin_required

GP = {'low_price': 100.0, 'high_price': 400.0, 'grid_count': 2,
      'stop_low_price': 50.0, 'stop_high_price': 500.0}
TIERS = [{'maxLeverage': 10, 'maxNotional': 1000.0},
         {'maxLeverage': 5, 'maxNotional': 5000.0}]


def test_breakdown_hand_computed():
    required, info = ladder_margin_required(70.0, 10.0, GP, 150.0, TIERS,
                                            k=1.25, fee_rate=0.001)
    assert info['L'] == 10
    assert info['ladder_total'] == pytest.approx(700.0)
    assert info['im'] == pytest.approx(70.0)
    assert info['worst_loss'] == pytest.approx(400.0)
    assert required == pytest.approx(1.25 * (70.0 + 400.0 + 0.7))


def test_k_scales_required():
    r1, _ = ladder_margin_required(70.0, 10.0, GP, 150.0, TIERS, k=1.25, fee_rate=0.001)
    r2, _ = ladder_margin_required(70.0, 10.0, GP, 150.0, TIERS, k=2.0, fee_rate=0.001)
    assert r2 == pytest.approx(r1 / 1.25 * 2.0)


def test_entry_below_band_all_sells_up_loss_dominates():
    # entry=90 < low=100：全部档位是卖单 → loss_up=3×500−700=800、loss_down=0
    required, info = ladder_margin_required(70.0, 10.0, GP, 90.0, TIERS,
                                            k=1.25, fee_rate=0.001)
    assert info['worst_loss'] == pytest.approx(800.0)
    assert required == pytest.approx(1.25 * (70.0 + 800.0 + 0.7))


def test_empty_tiers_returns_none():
    assert ladder_margin_required(70.0, 10.0, GP, 150.0, [], k=1.25) is None


def test_unbuildable_grid_returns_none():
    # cap=0 → order_num=0 → grid_order_info None → None（fail-closed 交回调用方）
    assert ladder_margin_required(0.0, 10.0, GP, 150.0, TIERS, k=1.25) is None


def test_min_amount_truncation_reflected():
    # min_amount=0.3 → order_num 1.0→0.9 → 整梯名义 630、IM 63、loss_up=0.9×400=360
    required, info = ladder_margin_required(70.0, 10.0, GP, 150.0, TIERS,
                                            min_amount=0.3, k=1.25, fee_rate=0.001)
    assert info['ladder_total'] == pytest.approx(630.0)
    assert info['im'] == pytest.approx(63.0)
    assert info['worst_loss'] == pytest.approx(360.0)
    assert required == pytest.approx(1.25 * (63.0 + 360.0 + 0.63))
