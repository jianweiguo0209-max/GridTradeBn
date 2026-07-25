"""判定窗档案补全(2026-07-25 用户令"全部补全,此后所有臂以补全后为准")。

**背景**:旧判定窗归档 1h 比 1m 全(HOLD-B:1h 337 币 vs 1m ~193 币)——选币走 1h、
开格走 1m,故"选中却无 1m"的币会被丢格。这对锚臂是历史既成事实(BASE_TD 即在此残缺
档案下产出),但对 p12 臂是**数据 artifact**:若 p12 偏好那批无 1m 的币,掉格会被误读成
选币器劣势。补全后所有臂在同一完整档案上比,才是干净对照。

**时序要点(重要)**:注入代码的保真度已在补全**之前**证死——parity 四层逐位 PASS +
HOLD-B 组合级锚 ret+1.58/Calmar4.1/格1169 逐位复现 BASE_TD。补全后锚不再等于 BASE_TD
(池变了)属预期,不构成"挪门柱":补全对锚臂与 p12 臂同等作用,主判据(预注册 §5)未动。

范围=判定六窗,各窗前推 PREHEAT_DAYS 天(cal_factor 需 max_candle_num=160 根 1h≈6.7 天,
留余量);warm_vision 幂等,已有天跳过。留出窗 HOLD-C/D 已是全量,不在此列。
用法: BT_VISION_WORKERS=6 nohup .venv/bin/python p12_warm_all.py > ablation/p12_warm_all.log 2>&1 &
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

WINDOWS = {                      # 判定六窗(留出 HOLD-C/D 已全量)
    'HOLD-B': ('2024-10-01', '2024-11-30'),
    'HOLD-A': ('2025-02-01', '2025-03-31'),
    'W1': ('2025-08-15', '2025-10-14'),
    'W2': ('2025-10-15', '2025-12-14'),
    'OOS': ('2026-01-01', '2026-02-28'),
    'IS': ('2026-03-01', '2026-06-30'),
}
PREHEAT_DAYS = 15                # > max_candle_num(160根1h≈6.7天)
TFS = ('1m', '1h', 'funding')


def _ms(ts):
    return int(pd.Timestamp(ts, tz='UTC').value // 1_000_000)


def main():
    cache = ParquetCache(V.default_cache_root())
    workers = int(os.environ.get('BT_VISION_WORKERS', '6'))
    bl = effective_blacklist((), DEFAULT_TIER_POLICY)
    universe = sorted(set(V.list_archive_symbols()) - set(bl))
    print('[warm] universe=%d workers=%d tfs=%s' % (len(universe), workers, TFS), flush=True)
    for wn, (s0, e0) in WINDOWS.items():
        t0 = time.time()
        lo = pd.Timestamp(s0) - pd.Timedelta(days=PREHEAT_DAYS)
        hi = pd.Timestamp(e0) + pd.Timedelta(days=1)
        print('[warm] %s %s~%s(含预热) 开跑 %s'
              % (wn, lo.date(), e0, time.strftime('%H:%M')), flush=True)
        st = V.warm_vision(cache, universe, _ms(lo), _ms(hi) - 1, timeframes=TFS,
                           workers=workers, log=lambda *a: None)
        print('[warm] %s DONE %.1fmin | 1m行=%d 跳过=%d 空天=%d'
              % (wn, (time.time() - t0) / 60, st['1m']['rows'],
                 st['skipped_cached'], st['empty_days']), flush=True)
    print('P12_WARM_ALL_DONE', flush=True)


if __name__ == '__main__':
    main()
