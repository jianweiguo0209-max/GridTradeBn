"""
网格成交仿真器（v1 原型）—— 回测出 PnL 的核心。

OKX 中性合约网格（create_grid: runType='2' 等比, direction='neutral'）的成交/盈亏仿真：
给定网格参数 + 价格路径(OHLC bars)，逐格模拟买卖成交，输出 pnlRatio 轨迹与平仓结果。

=== 建模假设（v1，务必知晓；fidelity 需用 gridResult.csv 校准）===
1. 网格线：等比(runType=2) L_k = min_px*(max_px/min_px)^(k/N)，共 N+1 条线、N 格。
2. 每格固定「基础数量」q = (sz*lever/entry_px)/N（等量模式）。OKX 实际可能等保证金分配，
   这是最大的待校准点之一。
3. 中性初始仓位：开网时按 entry_px 买入 q×(entry 上方的线数) 作为初始多头库存，
   以便价格上行时逐格卖出。价格下行则在下方线逐格补仓。
4. 成交价＝网格线价（无滑点/无部分成交）。手续费按成交额×fee_rate 双边收取。
5. bar 内路径近似：阳线假设 低→高，阴线假设 高→低（决定同一 bar 内成交先后）。
   1H bar 下此近似误差较大；接 1m 数据可显著降低（属已知 fidelity 上限）。
6. 终止：价格触及 sl_px(网格终止最低价)或 tp_px(网格终止最高价)即整网平仓；
   同一 bar 同时触及，保守取 SL 先（悲观口径）。pnlRatio 固定止盈损/回撤止盈见 apply_exit_rules。
7. 资金费、pv 爆量主动止损未建模（需 S2 资金费 + 1m 数据），属下一层。

pnlRatio = (已实现 + 浮动 - 手续费) / sz，与实盘 stop_loss.py 的 pnlRatio=totalPnl/sz 对齐。
"""
import math
from collections import deque


def build_grid_levels(min_px, max_px, grid_num, run_type='2'):
    """返回 N+1 条网格线（升序）。run_type '2'=等比, '1'=等差。"""
    n = int(grid_num)
    if run_type == '2' or run_type == 2:
        ratio = (max_px / min_px) ** (1.0 / n)
        return [min_px * (ratio ** k) for k in range(n + 1)]
    else:
        step = (max_px - min_px) / n
        return [min_px + step * k for k in range(n + 1)]


def _crossings(from_px, to_px, levels):
    """从 from_px 走到 to_px，按经过顺序返回 (level_price, direction)；
    direction: +1 上穿(卖), -1 下穿(买)。严格穿越（不含端点重复）。"""
    out = []
    if to_px > from_px:  # 上行
        for L in levels:
            if from_px < L <= to_px:
                out.append((L, +1))
    elif to_px < from_px:  # 下行
        for L in sorted(levels, reverse=True):
            if to_px <= L < from_px:
                out.append((L, -1))
    return out


def simulate_grid(params, bars, fee_rate=0.0005):
    """
    params: dict(min_px, max_px, grid_num, run_type, sz, lever, entry_px, tp_px, sl_px)
    bars:   迭代 dict/对象序列，每个含 high, low, close, 可选 open, ts（按时间升序）
    返回:   dict(realized, unrealized, fees, pnl, pnl_ratio, n_fills, terminated,
                 exit_reason, exit_px, pnl_ratio_series)
    """
    min_px = float(params['min_px']); max_px = float(params['max_px'])
    N = int(params['grid_num']); run_type = params.get('run_type', '2')
    sz = float(params['sz']); lever = float(params['lever'])
    entry = float(params['entry_px'])
    tp_px = float(params.get('tp_px')) if params.get('tp_px') not in (None, '') else float('inf')
    sl_px = float(params.get('sl_px')) if params.get('sl_px') not in (None, '') else 0.0

    levels = build_grid_levels(min_px, max_px, N, run_type)
    q = (sz * lever / entry) / N  # 每格基础数量

    # 中性初始库存：entry 上方的每条线一手，成本=entry（待价格上行卖出）
    inventory = deque()  # 元素: [buy_price, qty]，FIFO
    n_above = sum(1 for L in levels if L > entry)
    if n_above > 0:
        inventory.append([entry, q * n_above])

    realized = 0.0
    fees = 0.0
    n_fills = 0
    pnl_ratio_series = []
    terminated = False
    exit_reason = None
    exit_px = None

    def _sell(price):
        """卖出一格 q（先平最早库存）。"""
        nonlocal realized, fees, n_fills
        remaining = q
        while remaining > 1e-15 and inventory:
            lot = inventory[0]
            take = min(remaining, lot[1])
            realized += take * (price - lot[0])
            fees += take * price * fee_rate
            lot[1] -= take
            remaining -= take
            if lot[1] <= 1e-15:
                inventory.popleft()
        n_fills += 1

    def _buy(price):
        """买入一格 q（加库存）。"""
        nonlocal fees, n_fills
        inventory.append([price, q])
        fees += q * price * fee_rate
        n_fills += 1

    def _net_qty():
        return sum(lot[1] for lot in inventory)

    def _cost_basis_qty():
        return sum(lot[0] * lot[1] for lot in inventory)

    prev_px = entry
    for bar in bars:
        high = float(bar['high']); low = float(bar['low']); close = float(bar['close'])
        openp = float(bar['open']) if bar.get('open') not in (None, '') else prev_px

        # --- 终止检查（在本 bar 触发即整网平仓）---
        hit_sl = low <= sl_px
        hit_tp = high >= tp_px
        if hit_sl or hit_tp:
            # 同 bar 同时触发：保守取 SL 先
            exit_px = sl_px if hit_sl else tp_px
            exit_reason = '止损终止' if hit_sl else '止盈终止'
            # 先把到 exit_px 之前的格子成交也走一遍（简化：直接按 exit_px 平所有库存）
            net = _net_qty()
            realized += sum(lot[1] * (exit_px - lot[0]) for lot in inventory)
            fees += net * exit_px * fee_rate
            inventory.clear()
            pnl = realized - fees
            pnl_ratio_series.append(pnl / sz)
            terminated = True
            break

        # --- bar 内成交：按方向近似路径 ---
        up_bar = close >= openp
        if up_bar:
            sweeps = [(prev_px, low), (low, high), (high, close)]
        else:
            sweeps = [(prev_px, high), (high, low), (low, close)]
        for a, b in sweeps:
            for L, direction in _crossings(a, b, levels):
                if direction == +1:
                    if inventory:  # 有库存才卖
                        _sell(L)
                else:
                    _buy(L)
        prev_px = close

        # --- bar 末 mark-to-market ---
        unreal = _net_qty() * close - _cost_basis_qty()
        pnl = realized + unreal - fees
        pnl_ratio_series.append(pnl / sz)

    if not terminated:
        # 窗口结束仍未终止：按最后收盘 MTM（持仓未平）
        last_close = float(bars[-1]['close']) if len(bars) else entry
        unreal = _net_qty() * last_close - _cost_basis_qty()
        pnl = realized + unreal - fees
        exit_reason = '窗口结束'
        exit_px = last_close
    else:
        unreal = 0.0
        pnl = realized - fees

    return {
        'realized': realized,
        'unrealized': unreal,
        'fees': fees,
        'pnl': pnl,
        'pnl_ratio': pnl / sz,
        'n_fills': n_fills,
        'terminated': terminated,
        'exit_reason': exit_reason,
        'exit_px': exit_px,
        'pnl_ratio_series': pnl_ratio_series,
    }


def apply_exit_rules(pnl_ratio_series, stop_cfg):
    """
    在 pnlRatio 轨迹上套用实盘 stop_loss.py 的 pnlRatio 类退出（固定止盈损 + 回撤止盈 L1/L2）。
    返回最早触发的 (index, reason)；未触发返回 (None, None)。
    与 fixed_loss_and_profit 阈值对齐，保证 parity（资金费/pv 止损需额外数据，未含）。
    """
    sp = stop_cfg['stop_profit']; sl = stop_cfg['stop_loss']
    l1 = stop_cfg['stop_risk_l1']; l2 = stop_cfg['stop_risk_l2']
    pmax = -float('inf')
    for i, pr in enumerate(pnl_ratio_series):
        pmax = max(pmax, pr)
        if pr > sp:
            return i, '固定止盈'
        if pr < -sl:
            return i, '固定止损'
        if (pmax - pr >= l1) and (0.01 <= pmax < 0.02):
            return i, '回撤止盈L1'
        if (pmax - pr >= l2) and (pmax >= 0.02):
            return i, '回撤止盈L2'
    return None, None
