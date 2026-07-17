"""抓取每币下单量步长（stepSize）并缓存，供回测按实盘同款 TRUNCATE 取整。

    .venv/bin/python -m scripts.fetch_lot_sizes

产物：data/binance/_meta/lot_sizes.json（{symbol: step}）。data/ 在 .gitignore 内，故本地缓存、
**新克隆需跑一次**；缺文件时回测 fail-soft 退化为不取整（= 2026-07-18 之前的旧行为）。

为何需要：实盘 grid_executor 下单前走 adapter.quantize_amount → ccxt amount_to_precision（币安
精度模式下是 **TRUNCATE 向下**），回测此前硬传 min_amount=0.0 把引擎自带的同款截断关掉了。
详见 gridtrade/backtest/lot_sizes.py 的量级注记（cap=1000 下影响 ~0.01%，真正的失真在 cap 本身）。

只用公开行情接口（load_markets），无需 API key。
"""
import sys

import ccxt

from gridtrade.backtest import lot_sizes
from gridtrade.backtest import vision as V


def main():
    client = ccxt.binanceusdm({'enableRateLimit': True})
    markets = client.load_markets()
    out = {}
    for _native, m in markets.items():
        if not (m.get('swap') and m.get('quote') == 'USDT' and m.get('active')):
            continue
        step = (m.get('precision') or {}).get('amount')
        if step and float(step) > 0:          # step<=0 = 交易所未给 → 剔除（否则会取整成 0）
            out[m['symbol']] = float(step)
    if not out:
        print('未取到任何 stepSize —— 中止，不覆盖既有缓存', file=sys.stderr)
        return 1
    path = lot_sizes.save(V.default_cache_root(), out)
    print('已写 %s（%d 币）' % (path, len(out)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
