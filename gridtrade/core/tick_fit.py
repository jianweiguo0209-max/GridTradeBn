"""tickSize 票池过滤 —— 与回测 eff1_scan_v2.tick_filter 同语义。

背景(memory: tick-blindspot-is-eff1-edge):spacing<3×tick 的密格币回测虚高 3.9×、
实盘 41% 挂不上单;eff1 的全部回测读数只在 MIN_TICKS=3 下有效。缺 tick / 布网算不出
⇒ fail-open 保留(生产 rank 人群实测最小 23.2 tick,过滤对它是空转保险)。
在排名/截断**之前**调用 ⇒ 名次递补免费获得。
"""
from gridtrade.core.grid_params import calc_grid_params_v2


def _unfit(row, tick, cfg, min_ticks):
    if not tick or tick != tick:
        return False                                   # fail-open
    try:
        p = calc_grid_params_v2(row=row, price_limit=cfg['price_limit'],
                                stop_limit=cfg['stop_limit'],
                                v2_config=cfg.get('grid_v2_config', {}))
    except Exception:
        return False                                   # fail-open
    return (p['high_price'] - p['low_price']) / p['grid_count'] < min_ticks * tick


def filter_tick_fit(df, tick_map, strategy_config, min_ticks, log=None):
    """返回 (保留df, 剔除symbol列表)。min_ticks<=0 或空表 ⇒ 原样返回。"""
    if min_ticks <= 0 or df is None or df.empty or not tick_map:
        return df, []
    bad = [i for i, row in df.iterrows()
           if _unfit(row, tick_map.get(row['symbol']), strategy_config, min_ticks)]
    if not bad:
        return df, []
    dropped = sorted(df.loc[bad, 'symbol'].unique().tolist())
    if log:
        log('[tick-filter] -%d 币 spacing<%g×tick (e.g. %s)'
            % (len(dropped), min_ticks, dropped[:5]))
    return df.drop(index=bad), dropped
