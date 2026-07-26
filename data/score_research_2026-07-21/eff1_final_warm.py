"""eff1 终审双窗建库(2026-07-26,预注册 v3/v4 第三段)。

HOLD-F = 2024-08-01~2024-09-30(规则选窗:2024-10 前最近可完整下载双月;本地零覆盖)
JUL26  = 2026-07-03~2026-07-24(前向影子第一期;Vision 日档已发布末日=07-24,07-25 尚 404)
各窗前推 PREHEAT_DAYS=15(> cal_factor 需 max_candle_num=160 根 1h≈6.7 天)。

⚠硬约束(scan-brief):本脚本只建库(行情/标签/因子/POOL),**任何臂不得在这两窗上运行
直至 eff1-opt 冻结**(第二段 commit 之后)。裁决窗只见预注册三臂——27臂同场消费 HOLD-E 的教训。
用法: BT_VISION_WORKERS=6 nohup .venv/bin/python eff1_final_warm.py > ablation/eff1_final_warm.log 2>&1 &
"""
import os
import sys
import time

sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa: F401  锁线程

import pandas as pd

from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

WINDOWS = {'HOLD-F': ('2024-08-01', '2024-09-30'),
           'JUL26': ('2026-07-03', '2026-07-24')}
PREHEAT_DAYS = 15
TFS = ('1m', '1h', 'funding')


def _ms(ts):
    return int(pd.Timestamp(ts, tz='UTC').value // 1_000_000)


def main():
    cache = ParquetCache(V.default_cache_root())
    workers = int(os.environ.get('BT_VISION_WORKERS', '6'))
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    print('[warm] universe=%d workers=%d' % (len(universe), workers), flush=True)
    for wn, (s0, e0) in WINDOWS.items():
        t0 = time.time()
        lo = pd.Timestamp(s0) - pd.Timedelta(days=PREHEAT_DAYS)
        hi = pd.Timestamp(e0) + pd.Timedelta(days=1)
        print('[warm] %s %s~%s(含预热) 开跑 %s'
              % (wn, lo.date(), e0, time.strftime('%H:%M')), flush=True)
        st = V.warm_vision(cache, universe, _ms(lo), _ms(hi) - 1, timeframes=TFS,
                           workers=workers, log=lambda *a: None)
        print('[warm] %s DONE %.1fmin | 1m行=%d 1h行=%d 跳过=%d 空天=%d'
              % (wn, (time.time() - t0) / 60, st['1m']['rows'], st['1h']['rows'],
                 st['skipped_cached'], st['empty_days']), flush=True)
    print('EFF1_FINAL_WARM_DONE', flush=True)


if __name__ == '__main__':
    main()
