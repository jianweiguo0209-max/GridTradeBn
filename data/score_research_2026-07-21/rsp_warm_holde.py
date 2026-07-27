"""HOLD-E 档案补齐(2026-07-26,RSP111 战役唯一裁决窗)。

窗 = 2025-06-01 ~ 2025-08-14(全库唯一未触碰近代时段)。
本地缺 2025-06、2025-07 整月(2025-05 有 449 币可作预热、2025-08 有 495 币)。
预热前推 15 天(> cal_factor 需 max_candle_num=160 根 1h ≈6.7 天)。
warm_vision 幂等:整月全命中即 skipped_cached。

⚠时序纪律:本脚本只应在预注册 commit(664bc19)之后运行 —— brief §3.3 要求
"预注册在 HOLD-E 数据构建之前写死"。
用法: BT_VISION_WORKERS=6 nohup .venv/bin/python rsp_warm_holde.py > ablation/rsp_warm_holde.log 2>&1 &
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

S0, E0 = '2025-06-01', '2025-08-14'
PREHEAT_DAYS = 15
TFS = ('1m', '1h', 'funding')


def _ms(ts):
    return int(pd.Timestamp(ts, tz='UTC').value // 1_000_000)


def main():
    cache = ParquetCache(V.default_cache_root())
    workers = int(os.environ.get('BT_VISION_WORKERS', '6'))
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    lo = pd.Timestamp(S0) - pd.Timedelta(days=PREHEAT_DAYS)
    hi = pd.Timestamp(E0) + pd.Timedelta(days=1)
    print('[warm] HOLD-E %s~%s(含预热) universe=%d workers=%d'
          % (lo.date(), E0, len(universe), workers), flush=True)
    t0 = time.time()
    st = V.warm_vision(cache, universe, _ms(lo), _ms(hi) - 1, timeframes=TFS,
                       workers=workers, log=lambda *a: None)
    print('[warm] DONE %.1fmin | 1m行=%d 1h行=%d funding行=%d 跳过=%d 空天=%d'
          % ((time.time() - t0) / 60, st['1m']['rows'], st['1h']['rows'],
             st['funding']['rows'], st['skipped_cached'], st['empty_days']), flush=True)
    print('RSP_WARM_HOLDE_DONE', flush=True)


if __name__ == '__main__':
    main()
