"""MarginGate 交易所保证金口径纯函数（币安原生,spec 2026-07-19-binance-native-leverage;
初版 2026-07-18-margin-gate-exchange-im 的「双侧/L」IM 项已按三态 demo 实测修正）。

币安 USDM 初始保证金挂单时刻锁定,仓位与挂单分记但净额抵扣——总 IM 在网格任意轨迹
状态**恒 = max(Σ买侧, Σ卖侧)/L**(单侧恒等式,open_order_im_notional 净额规则推论,
三态实测 $13.15/$13.48≈单侧 $13.38)。可用余额同时要扛沿途浮亏与手续费。故:

    required = k × (单侧名义/L + worst止损浮亏 + 整梯名义×fee_rate)

L 与 executor.open 同源预演:pick_leverage_max(单侧×BRACKET_HEADROOM)——门链在
set_leverage 之前跑,必须模拟将要设的那个 L。返回 None = 无法计算（tiers 空 /
建网 None / L None），调用方 fail-closed 回退 cap 口径。
"""
from gridtrade.core.grid_engine import grid_order_info
from gridtrade.execution.leverage_policy import (BRACKET_HEADROOM, pick_leverage_max,
                                                 worst_side_notional)

DEFAULT_K = 1.25            # 安全余量：标记价漂移/维持保证金缓冲（大项 IM/浮亏/fee 已显式）
DEFAULT_FEE_RATE = 0.0005   # VIP0 taker 上界（maker 0.0002）；ε 项，量级 <$2/梯


def ladder_margin_required(cap, gearing, grid_params, entry, tiers, *,
                           min_amount=0.0, k=DEFAULT_K, fee_rate=DEFAULT_FEE_RATE):
    """→ (required, breakdown) 或 None（无法计算，调用方回退 cap 口径）。"""
    if not tiers:
        return None
    gp = grid_params
    gi = grid_order_info(cap, gearing, gp['low_price'], gp['high_price'],
                         int(gp['grid_count']), gp['stop_low_price'],
                         gp['stop_high_price'], min_amount=min_amount, max_rate=1.0)
    if gi is None:
        return None
    n = float(gi['每笔数量'])
    prices = [float(p) for p in gi['价格序列']]
    entry = float(entry)
    ladder_total = n * sum(prices)                     # = cap×gearing（min_amount 截断前）
    side = worst_side_notional(prices, n, entry)       # 单侧最坏名义(恒等式基)
    L = pick_leverage_max(side * BRACKET_HEADROOM, tiers)   # 与 grid_executor.open 同源 → 同 L
    if not L:
        return None
    im = side / float(L)
    # 浮亏 = 库存×(均价−止损价)，展开成 Σ(p−stop) 精确式；两侧取大（对齐最坏扫带方向）
    loss_down = n * sum(p - float(gp['stop_low_price']) for p in prices if p < entry)
    loss_up = n * sum(float(gp['stop_high_price']) - p for p in prices if p > entry)
    worst_loss = max(loss_down, loss_up, 0.0)
    fee = ladder_total * float(fee_rate)
    required = float(k) * (im + worst_loss + fee)
    return required, {'L': int(L), 'im': im, 'worst_loss': worst_loss,
                      'fee': fee, 'ladder_total': ladder_total, 'k': float(k)}
