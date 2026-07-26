"""标定样本覆盖了多宽的流动性区间?—— fill_rate=1.004 到底为哪一段背书。只读。"""
import sys, json, math
sys.path.insert(0, '/Users/thomaschang/Projects/GridTradeBi')
import gridtrade.backtest  # noqa
import pandas as pd
from gridtrade.backtest import vision as V
from gridtrade.backtest.cache import ParquetCache

cache = ParquetCache(V.default_cache_root())
f = pd.read_parquet('data/score_research_2026-07-21/ablation/fillrate_calib.parquet')
raw = open('/tmp/live_fills3.json').read(); raw = raw[raw.index('['):]
d = pd.DataFrame(json.loads(raw))
d['t0'] = pd.to_datetime(pd.to_numeric(d['created_at']), unit='ms')
t0 = d.groupby('symbol')['t0'].min()

rows = []
for sym, g in f.groupby('symbol'):
    rt = t0.get(sym)
    if rt is None:
        continue
    lo, hi = (rt - pd.Timedelta('3D')).date(), rt.date()
    try:
        h = cache.read_days_range('1h', sym, str(lo), str(hi))
    except Exception:
        h = None
    if h is None or h.empty:
        continue
    sub = h[h['candle_begin_time'] < rt]
    if len(sub) < 24:
        continue
    v24 = float(sub.tail(24)['quote_volume'].sum())
    rows.append({'symbol': sym, 'vol24_M': v24 / 1e6, 'n_grids': len(g),
                 'per_line_usd': (g['order_num'] * g['entry']).median(),
                 'live_qty': g['live_qty_equiv'].sum(), 'theo': g['theo_x'].sum()})
r = pd.DataFrame(rows).sort_values('vol24_M')
r['rate'] = r['live_qty'] / r['theo']
# 单线名义 占 该币每分钟成交额 的比例 = 参与率需求
r['参与率需求%'] = r['per_line_usd'] / (r['vol24_M'] * 1e6 / 1440) * 100
print('标定样本覆盖 %d 个币 / %d 格' % (len(r), int(r['n_grids'].sum())))
print('  24h 成交额: min=$%.2fM  q25=$%.1fM  median=$%.1fM  max=$%.0fM'
      % (r['vol24_M'].min(), r['vol24_M'].quantile(.25),
         r['vol24_M'].median(), r['vol24_M'].max()))
print('  参与率需求(单线名义/每分钟量): median=%.4f%%  max=%.4f%%'
      % (r['参与率需求%'].median(), r['参与率需求%'].max()))
print('\n最低流动性的 8 个币(标定的下沿在哪):')
print(r.head(8).to_string(index=False, float_format=lambda x: '%.4f' % x))
print('\n按流动性分半:')
med = r['vol24_M'].median()
for lab, s in (('低半', r[r['vol24_M'] < med]), ('高半', r[r['vol24_M'] >= med])):
    print('  %s (中位$%.1fM, %d币): fill_rate=%.3f  参与率需求中位=%.4f%%'
          % (lab, s['vol24_M'].median(), len(s),
             s['live_qty'].sum() / s['theo'].sum(), s['参与率需求%'].median()))
