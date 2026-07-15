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
