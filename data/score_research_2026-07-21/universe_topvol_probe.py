"""FLOW 这类小币为何能过 top55%:量出该过滤的**绝对**门槛。只读。"""
import sys, math
sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa
import pandas as pd
from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache
from gridtrade.config import DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist

cache = ParquetCache(V.default_cache_root())
bl = set(effective_blacklist((), DEFAULT_TIER_POLICY))
universe = sorted(set(V.list_archive_symbols()) - bl)
for RT in (pd.Timestamp('2026-01-15 00:00'), pd.Timestamp('2026-07-05 00:00')):
    lo, hi = (RT - pd.Timedelta('3D')).date(), RT.date()
    vols = {}
    for s in universe:
        try:
            df = cache.read_days_range('1h', s, str(lo), str(hi))
        except Exception:
            df = None
        if df is None or df.empty:
            continue
        sub = df[df['candle_begin_time'] < RT]
        if len(sub) < 24:
            continue
        vols[s] = float(sub.tail(24)['quote_volume'].sum())
    if not vols:
        print('%s 无数据' % RT); continue
    v = pd.Series(vols).sort_values(ascending=False)
    N = len(v); keep = max(1, math.ceil(0.55 * N))
    cut = v.iloc[keep - 1]
    print('=' * 74)
    print('run_time %s   合格币 N=%d  → 保留前 ceil(0.55N)=%d' % (RT, N, keep))
    print('  55%% 门槛(第 %d 名)的 24h 成交额 = $%.2fM' % (keep, cut / 1e6))
    print('  入选区间: 第1名 $%.0fM  ...  第%d名 $%.2fM   (跨 %.0f 倍)'
          % (v.iloc[0] / 1e6, keep, cut / 1e6, v.iloc[0] / cut))
    print('  中位数 $%.2fM   最小 $%.3fM' % (v.median() / 1e6, v.min() / 1e6))
    for tgt in ('FLOW/USDT:USDT', 'BTC/USDT:USDT', 'ETH/USDT:USDT'):
        if tgt in v.index:
            r = int(v.index.get_loc(tgt)) + 1
            print('  %-16s 第 %3d/%d 名 (前 %4.1f%%)  24h=$%.2fM   %s'
                  % (tgt.split('/')[0], r, N, r / N * 100, v[tgt] / 1e6,
                     '✅入选' if r <= keep else '❌淘汰'))
    print('  入选池里最小的 5 个:')
    for s, x in v.iloc[keep - 5:keep].items():
        print('     %-16s $%.2fM' % (s.split('/')[0], x / 1e6))
