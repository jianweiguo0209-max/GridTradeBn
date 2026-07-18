"""开格设杠杆纯函数（币安原生口径,spec 2026-07-19-binance-native-leverage;
取代 2026-07-15 的 HL 移植语义「双侧名义+减一档」）。

三块实测地基:①全仓模式所设 L 不影响强平(MMR 只按名义档)→ L 是纯押金效率旋钮,取最高;
②总 IM 全轨迹恒 = max(Σ买,Σ卖)/L(三态 demo 实测,净额规则 open_order_im_notional);
③档位只约束仓位名义:单侧×BRACKET_HEADROOM ≤ 档 maxNotional 即防 -2027。
tiers = [{'maxLeverage': int, 'maxNotional': float}]（adapter.fetch_leverage_tiers 产出）。"""

BRACKET_HEADROOM = 1.2   # 单侧名义的撞档余量(标记价漂移/回补时序;spec 地基③)


def cap_at_leverage(tiers, L):
    """设定杠杆 L 时的最大可持名义 = maxLev>=L 的最大 maxNotional；无匹配 → 0.0。"""
    vals = [t['maxNotional'] for t in tiers if t['maxLeverage'] >= L]
    return max(vals) if vals else 0.0


def worst_side_notional(prices, qty, entry):
    """单侧最坏名义 = max(Σ买侧, Σ卖侧)×qty——中性网格净仓与同向敞口全程 ≤ 单侧
    (IM 轨迹恒等式,spec 地基②),选档与 IM 备付均以此为基。"""
    entry = float(entry)
    buys = sum(float(p) for p in prices if float(p) < entry)
    sells = sum(float(p) for p in prices if float(p) > entry)
    return max(buys, sells) * float(qty)


def pick_leverage_max(need_notional, tiers):
    """能容 need(=单侧×BRACKET_HEADROOM,调用方显式乘)的**最高档** maxLev——不再减一档
    (全仓 L 不影响强平,取最高=押金最少;spec 地基①)。tiers 空 → None(fail-open,不设杠杆);
    全不容 → 最低档尽力(调用方按 cap_at_leverage<need 告警,-2027 由 open_proposals 隔离)。"""
    if not tiers:
        return None
    ok = [t['maxLeverage'] for t in tiers if t['maxNotional'] >= float(need_notional)]
    return int(max(ok)) if ok else int(min(t['maxLeverage'] for t in tiers))


def open_order_im_notional(buy_notional, sell_notional, pos_notional):
    """币安 openOrderIM 名义口径净额规则(4 状态 demo 实测逆向精确拟合,spec 地基②):
    多仓 p≥0: max(Σ买, max(0, Σ卖−2p));空仓对称。推论:总 IM(=仓位+挂单)在网格任意
    轨迹状态恒 = max(B,S)——单侧恒等式,MarginGate IM 项的依据。"""
    b, s, p = float(buy_notional), float(sell_notional), float(pos_notional)
    if p >= 0:
        return max(b, max(0.0, s - 2.0 * p))
    return max(s, max(0.0, b - 2.0 * abs(p)))


def normalize_tiers_map(raw):
    """ccxt bulk fetch_leverage_tiers 原始映射 → {sym: [{'maxLeverage':int,'maxNotional':float}]}。
    缺 maxLeverage 的档丢弃、空档位币丢弃。实盘缓存(binance.fetch_max_leverages)与回测
    (exclude_low_leverage)共用(单一事实源)。"""
    out = {}
    for sym, brs in (raw or {}).items():
        norm = [{'maxLeverage': int(t['maxLeverage']),
                 'maxNotional': float(t.get('maxNotional') or 0.0)}
                for t in (brs or []) if t.get('maxLeverage')]
        if norm:
            out[sym] = norm
    return out


def eligible_min_leverage(symbols, tiers_map, notional, gearing, min_lev):
    """票池杠杆预过滤：**第一档(币安最高档)最大杠杆** < min_lev 的币在选币前剔除——低杠杆档
    是币安对高危币(小市值/薄盘/易插针,维持率高)的风险分级,粗筛掉它们;开仓贴边的精筛由
    MarginGate 兜底(二者解耦)。

    2026-07-19 修正:此前用 `pick_leverage(notional)` 的**减一档**值判,把第一档=10x(减一档后
    pick_L<10)甚至第一档=20x 的正常币误剔(实测 169 剔除里 137 个是 10x、8 个是 20x,真正
    第一档<10 的只有 24)。「减一档留余量」是**开仓选杠杆**的安全逻辑(pick_leverage,不动),
    拿来做票池过滤会过度筛选。判据改回币安第一档档位。notional/gearing 保留签名(调用处传参)
    但过滤不再依赖。
    min_lev<=0=停用;tiers 缺失 → 保留(fail-open,与 maxlev 分级同语义)。返回 (kept, dropped)。"""
    if min_lev is None or min_lev <= 0:
        return list(symbols), []
    kept, dropped = [], []
    for s in symbols:
        tiers = (tiers_map or {}).get(s)
        if not tiers:
            kept.append(s)
            continue
        max_lev = max(int(t['maxLeverage']) for t in tiers)   # 第一档=币安给的最高杠杆
        (kept if max_lev >= min_lev else dropped).append(s)
    return kept, dropped


# pick_leverage(双侧+减一档)与 feasible(ceil(gearing) 档判定)已删——HL 移植语义,
# 由 pick_leverage_max/worst_side_notional 取代(spec 2026-07-19-binance-native-leverage)。
