"""开格设杠杆纯函数（spec 2026-07-15-open-set-leverage §3.2）。

币安杠杆档位：在设定杠杆 L 时最大可持名义 = maxLev>=L 的最大 maxNotional（杠杆越高档位越小）。
worst 名义 ≈ gearing×cap。pick_leverage 取"能容 worst 的最紧档的下一档"（减一档留余量），
clamp[ceil(gearing)（保证金撑得住 gearing×cap 名义所需最低杠杆）, 最高档 maxLev]。
tiers = [{'maxLeverage': int, 'maxNotional': float}]（adapter.fetch_leverage_tiers 产出）。"""
import math


def cap_at_leverage(tiers, L):
    """设定杠杆 L 时的最大可持名义 = maxLev>=L 的最大 maxNotional；无匹配 → 0.0。"""
    vals = [t['maxNotional'] for t in tiers if t['maxLeverage'] >= L]
    return max(vals) if vals else 0.0


def feasible(worst_notional, tiers, gearing):
    """worst 名义能否在 ceil(gearing) 杠杆下持有（保证金撑得住）。tiers 空 → True
    （fail-open，不因读不到档位而判死/告警）。仅供告警，不做排除（块 D 暂缓）。"""
    if not tiers:
        return True
    return worst_notional <= cap_at_leverage(tiers, math.ceil(float(gearing)))


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


def pick_leverage(worst_notional, tiers, gearing):
    """能容 worst 名义的最紧档的下一档 maxLev（减一档留余量），clamp[ceil(gearing), 最高档 maxLev]。
    tiers 空 → None（fail-open，调用方不设杠杆）。worst 超所有档（不可行）→ 最低档尽力（feasible 告警）。"""
    if not tiers:
        return None
    brs = sorted(tiers, key=lambda t: -t['maxLeverage'])   # 高杠杆(小名义)在前
    floor = math.ceil(float(gearing))
    top = brs[0]['maxLeverage']                            # 最高档 = symbol maxLev
    idx = next((i for i, b in enumerate(brs) if b['maxNotional'] >= worst_notional), None)
    if idx is None:                                        # worst 超所有档(不可行) → 最低档尽力
        raw = brs[-1]['maxLeverage']
    else:
        raw = brs[min(idx + 1, len(brs) - 1)]['maxLeverage']   # 减一档
    return int(min(max(raw, floor), top))
