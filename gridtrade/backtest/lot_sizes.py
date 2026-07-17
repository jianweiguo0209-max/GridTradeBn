"""每币下单量步长（stepSize）缓存 —— 让回测按实盘同款 TRUNCATE 取整。

分歧（2026-07-18 修）：
- **实盘**：`grid_executor` 下单前走 `adapter.quantize_amount` → ccxt `amount_to_precision`，
  币安精度模式下是 **TRUNCATE 向下**，故 `wire_qty <= order_num` 恒成立、**永不向上**。
- **回测（旧）**：`backtest_run` 硬传 `min_amount=0.0`，把引擎自带的取整
  （`grid_order_info`: `order_num - order_num % min_amount`，正是同款截断）**关掉了**
  → 用原始 float，系统性高估下单量。

量级注记：cap=1000（回测恒用值）下缩量中位仅 ~0.01%、均值 ~0.05% —— 交易所把低价币配粗步长
（59% 的币 step=1.0）但那些币 order_num 本就大，高价币则配细步长（BTC=0.001），故自然对齐。
**真正的失真不在这里，而在 cap 本身**：回测恒用 1000，实盘 `cap=clamp(equity×0.2451, 20, 1e5)`；
回测的尺度无关性正是实盘会破的地方（cap=$24.5 时缩量中位 0.40%、8.5% 的格缩超 5%）。

数据来自 `adapter.list_instruments()` 的 `precision.amount`；一次抓取缓存成 JSON，回测离线读。
**fail-soft**：缺文件/缺币 → 0.0 = 不取整 = 旧行为（宁可退化也不阻塞回测）。
"""
import json
import os

_REL = os.path.join('_meta', 'lot_sizes.json')


def path_for(cache_root):
    return os.path.join(cache_root, _REL)


def save(cache_root, lot_by_sym):
    """写 {symbol: step} JSON。"""
    p = path_for(cache_root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump({k: float(v) for k, v in lot_by_sym.items()}, f, indent=1, sort_keys=True)
    return p


def load(cache_root):
    """读缓存；缺失/损坏 → {}（fail-soft，调用方据此退化为不取整）。"""
    try:
        with open(path_for(cache_root), encoding='utf-8') as f:
            return {k: float(v) for k, v in json.load(f).items()}
    except Exception:
        return {}


def fetch_from_adapter(adapter):
    """adapter.list_instruments() → {symbol: step}。只留 step>0 的（0=交易所未给/未知）。"""
    out = {}
    for ins in adapter.list_instruments():
        lot = float(getattr(ins, 'lot', 0.0) or 0.0)
        if lot > 0:
            out[ins.symbol] = lot
    return out
