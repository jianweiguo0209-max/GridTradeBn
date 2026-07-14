"""保险丝覆盖率策略（纯函数；spec 2026-07-15-fuse-coverage-guard）。

保险丝 = 每格两张 reduce-only STOP_MARKET，数量 worst=满仓最大持仓。币安
MARKET_LOT_SIZE.maxQty 限制单笔市价单数量：worst>maxQty 时下单被 -4005 拒（ed4616e 起
适配器封顶到 maxQty，代价是超出部分无原生硬保护）。本模块给出"开仓前把 cap 降到丝能护
全额"的口径。

主网现状（2026-07-15 实测）：票池最小市价名义上限 $30,570 > 满仓名义额 ⇒ 恒不触发
（临界权益 ≈$36,684）；权益长大后自动接管。demo 的 maxQty 比主网小 3-1200 倍 ⇒ 会真实触发。
"""
from gridtrade.core.grid_engine import grid_order_info


def fuse_worst(cap, gearing, grid_params, min_amount=0.0):
    """满仓最大持仓量 worst = 每笔数量 × grid_count。
    口径与 executor.open 同源（grid_executor.py:81-84，**max_rate=1.0**，非回测的 0.68）。
    cap 太低建不了网 → None（调用方 fail-open）。"""
    gp = grid_params
    gi = grid_order_info(float(cap), float(gearing), gp['low_price'], gp['high_price'],
                         int(gp['grid_count']), gp['stop_low_price'], gp['stop_high_price'],
                         min_amount=float(min_amount), max_rate=1.0)
    if gi is None:
        return None
    return float(gi['每笔数量']) * int(gp['grid_count'])


def fuse_capped_cap(cap, gearing, grid_params, market_max_qty, *,
                    min_amount=0.0, min_coverage=1.0):
    """返回 (cap', coverage)。coverage = maxQty/worst（1.0=足额；None=未知/不可算）。

    min_coverage 是**干预触发阈值**——一旦干预就降到足额（coverage'=1.0），不存在"降到
    80% 就收手"的中间态（那既不省仓位又不护全额）。min_coverage<=0 = 停用（仅算 coverage
    供审计）。

    fail-open：maxQty 未知（<=0）或建不了网 → 原样返回、coverage=None，绝不因限额表读不到
    而干预（交易所自会校验；MinNotionalGate 兜底拒建不了网的 cap）。"""
    cap = float(cap)
    mx = float(market_max_qty or 0.0)
    if mx <= 0:
        return cap, None
    worst = fuse_worst(cap, gearing, grid_params, min_amount)
    if worst is None or worst <= 0:
        return cap, None
    coverage = mx / worst
    if float(min_coverage) <= 0 or coverage >= float(min_coverage):
        return cap, coverage
    cap2 = cap * coverage          # order_num 随 cap 线性 ⇒ worst' = maxQty（coverage'=1.0）
    w2 = fuse_worst(cap2, gearing, grid_params, min_amount)
    # min_amount 向下取整只减不增 ⇒ 必然成立；仍断言防未来 grid_order_info 改动悄悄破坏
    if w2 is not None and w2 > mx * (1 + 1e-9):
        raise AssertionError('fuse cap-down 失效: worst=%.8g > maxQty=%.8g' % (w2, mx))
    return cap2, coverage


def audit_fuse_coverage(universe, prices, max_qtys, cap, gearing):
    """票池级保险丝覆盖审计（近似口径，spec §一）：满仓名义额 ≈ cap×gearing；
    某币足额 ⟺ maxQty×price ≥ 满仓名义额。

    返回 {'need': 满仓名义额, 'total': 参与审计的币数, 'short': [(symbol, coverage)…]}
    （short 按覆盖率升序）。缺价/缺 maxQty 的币跳过（不参与审计，不误报）。
    用途：让"逼近临界权益"提前可见——报出不足额币即"实盘几何开始偏离回测"的信号（§七）。"""
    need = float(cap) * float(gearing)
    short = []
    total = 0
    for s in universe:
        mx = float((max_qtys or {}).get(s) or 0.0)
        px = float((prices or {}).get(s) or 0.0)
        if mx <= 0 or px <= 0 or need <= 0:
            continue
        total += 1
        cov = mx * px / need
        if cov < 1.0:
            short.append((s, cov))
    short.sort(key=lambda x: x[1])
    return {'need': need, 'total': total, 'short': short}
