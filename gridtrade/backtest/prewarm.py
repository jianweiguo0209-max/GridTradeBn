"""预热：按配置交易所/票池/窗口把 DataSource 缓存填满，预热后回测离线。"""


def resolve_universe(datasource, *, quote='USDT', min_list_age_days=15, limit=None):
    out = []
    for inst in datasource.list_instruments():
        if inst.state != 'live':
            continue
        # list_ts==0 视为未知，放行；否则可按需扩展上市时长过滤（此处保留接口）
        sym = inst.symbol
        if (':%s' % quote) in sym or ('/%s:' % quote) in sym or sym.endswith('/%s' % quote):
            out.append(sym)
        else:
            out.append(sym)  # 规范符号已由 adapter 统一；不强制 quote 形态
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
