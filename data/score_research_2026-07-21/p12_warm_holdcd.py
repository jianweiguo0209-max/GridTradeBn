"""p12 组合战役档案补齐(2026-07-25):HOLD-C/HOLD-D 两新留出窗 vision 下载。

新留出窗本地零覆盖(HOLD-C 2025-04/05、HOLD-D 2024-12/2025-01 各 0 币)——按 brief §3
「缺月用 vision 下载器补」。**只下窗月本身**(不加 buffer),与 HOLD-A/B 归档形状一致:
HOLD-A=2025-02/03 only、HOLD-B=2024-10/11 only,冷因子起手是既定 byte-exact 口径,
加 buffer 月反而改变因子 warmup、破坏与既有留出的方法论一致性。label -24h 回看与 +12h
前看所需邻月(2024-11/2025-03)已在档,天然覆盖。

warm_vision 幂等:整月命中即 skipped_cached;未发布不落哨兵。ThreadPool I/O 为主,
与在跑 cf_run(CPU单核)不抢核;BT_VISION_WORKERS 保守(默认此处6)控内存峰值。
用法: BT_VISION_WORKERS=6 nohup .venv/bin/python data/score_research_2026-07-21/p12_warm_holdcd.py > .../p12_warm.log 2>&1 &
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

# 只下窗月(与 HOLD-A/B 归档形状一致);邻月 2024-11/2025-03 已在档供 label 回看
WINDOWS = {
    'HOLD-D': ('2024-12-01', '2025-01-31'),
    'HOLD-C': ('2025-04-01', '2025-05-31'),
}
TFS = ('1m', '1h', 'funding')


def _ms(s):
    return int(pd.Timestamp(s, tz='UTC').value // 1_000_000)


def main():
    cache = ParquetCache(V.default_cache_root())
    workers = int(os.environ.get('BT_VISION_WORKERS', '6'))
    print('[warm] 列举归档 symbol 全集...', flush=True)
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)   # 与 geo_final/holdout_gate 同口径
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    print('[warm] universe=%d(黑名单已剔) workers=%d tfs=%s'
          % (len(universe), workers, TFS), flush=True)
    for wn, (s0, e0) in WINDOWS.items():
        t0 = time.time()
        # 只覆盖窗月(end=窗末日末刻,不触下月);label -24h 回看邻月已在档
        start_ms = _ms(s0)
        end_ms = _ms(pd.Timestamp(e0) + pd.Timedelta(days=1)) - 1
        print('[warm] %s %s~%s 开跑 %s' % (wn, s0, e0, time.strftime('%H:%M')), flush=True)
        st = V.warm_vision(cache, universe, start_ms, end_ms, timeframes=TFS,
                           workers=workers, log=lambda *a: None)
        print('[warm] %s DONE %.1fmin | %s' % (wn, (time.time() - t0) / 60, st), flush=True)
    print('P12_WARM_DONE', flush=True)


if __name__ == '__main__':
    main()
