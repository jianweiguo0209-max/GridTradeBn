"""MarginGate 交易所保证金口径纯函数（spec 2026-07-18-margin-gate-exchange-im）。

币安 USDM 初始保证金在**挂单时刻**锁定（open order IM，one-way 净额按最坏侧），成交转
仓位 IM。沿「挂满梯 → 吃满最坏侧」轨迹，总 IM（挂单+仓位）因回售补单单调升至
≈ 整梯双侧名义/L（MET 实测 $64.5→$127.7，2026-07-18）；可用余额同时要扛沿途浮亏
（库存×|成交均价−止损价|，与 IM 同量级）与手续费。故：

    required = k × (整梯双侧名义/L + worst止损浮亏 + 整梯名义×fee_rate)

L 与 executor.open 同源预演：pick_leverage(order_num×grid_count×entry)——门链在
set_leverage 之前跑，必须模拟将要设的那个 L。返回 None = 无法计算（tiers 空 /
建网 None / L None），调用方 fail-closed 回退 cap 口径。
"""
from gridtrade.core.grid_engine import grid_order_info
from gridtrade.execution.leverage_policy import pick_leverage

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
    worst_exec = n * int(gp['grid_count']) * entry     # 与 grid_executor.open 同式 → 同 L
    L = pick_leverage(worst_exec, tiers, gearing)
    if not L:
        return None
    im = ladder_total / float(L)
    # 浮亏 = 库存×(均价−止损价)，展开成 Σ(p−stop) 精确式；两侧取大（对齐最坏扫带方向）
    loss_down = n * sum(p - float(gp['stop_low_price']) for p in prices if p < entry)
    loss_up = n * sum(float(gp['stop_high_price']) - p for p in prices if p > entry)
    worst_loss = max(loss_down, loss_up, 0.0)
    fee = ladder_total * float(fee_rate)
    required = float(k) * (im + worst_loss + fee)
    return required, {'L': int(L), 'im': im, 'worst_loss': worst_loss,
                      'fee': fee, 'ladder_total': ladder_total, 'k': float(k)}
