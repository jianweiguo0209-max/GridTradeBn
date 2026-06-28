"""预热：按配置交易所/票池/窗口把 DataSource 缓存填满，预热后回测离线。"""


def resolve_universe(datasource, *, quote='USDT', min_list_age_days=15, limit=None):
    """返回可交易票池（规范符号）。当前仅过滤 state=='live' + 去重排序 + 可选 limit。
    quote / min_list_age_days 为**预留参数、暂未生效**：符号已由 adapter 统一为规范形态，
    且 list_ts 多为 0/未知，无可靠上市时长可过滤；待 adapter 提供可靠 list_ts 后再实现。"""
    out = [inst.symbol for inst in datasource.list_instruments() if inst.state == 'live']
    out = sorted(set(out))
    return out[:limit] if limit else out


def prewarm_ohlcv(datasource, universe, start_ms, end_ms, *, log=print):
    total = 0
    n = 0
    for s in universe:
        df = datasource.fetch_ohlcv_range(s, start_ms, end_ms)
        total += int(len(df))
        n += 1
        if n % 25 == 0:
            log('[prewarm] ohlcv %d/%d' % (n, len(universe)))
    return {'symbols': n, 'rows': total}


def prewarm_funding(datasource, universe, start_ms, end_ms, *, log=print):
    total = 0
    n = 0
    for s in universe:
        df = datasource.fetch_funding_range(s, start_ms, end_ms)
        total += int(len(df))
        n += 1
    return {'symbols': n, 'rows': total}
