"""预热：按配置交易所/票池/窗口把 DataSource 缓存填满，预热后回测离线。"""


def resolve_universe(datasource, *, blacklist=(), quote='USDT', min_list_age_days=15, limit=None):
    """返回可交易票池（规范符号）：state=='live' −黑名单，去重排序 + 可选 limit。
    quote / min_list_age_days 为**预留参数、暂未生效**（list_ts 多为 0/未知）。"""
    bl = set(blacklist)
    out = [inst.symbol for inst in datasource.list_instruments()
           if inst.state == 'live' and inst.symbol not in bl]
    out = sorted(set(out))
    return out[:limit] if limit else out


def prewarm_ohlcv(datasource, universe, start_ms, end_ms, *, log=print):
    total = 0
    n = 0
    skipped = 0
    first_err = None
    for s in universe:
        try:
            df = datasource.fetch_ohlcv_range(s, start_ms, end_ms)
        except Exception as exc:      # 坏币(ccxt BadSymbol/无数据/拉取失败)跳过，不中断全池
            skipped += 1              # 全市场含少量不可拉取币；镜像 live fetch_universe_candles
            if first_err is None:
                first_err = '%s -> %r' % (s, exc)
            continue
        total += int(len(df))
        n += 1
        if n % 25 == 0:
            log('[prewarm] ohlcv %d/%d' % (n, len(universe)))
    if skipped:
        log('[prewarm] ohlcv skipped %d symbols (e.g. %s)' % (skipped, first_err))
    return {'symbols': n, 'rows': total, 'skipped': skipped}


def prewarm_funding(datasource, universe, start_ms, end_ms, *, log=print):
    total = 0
    n = 0
    skipped = 0
    first_err = None
    for s in universe:
        try:
            df = datasource.fetch_funding_range(s, start_ms, end_ms)
        except Exception as exc:      # 坏币跳过，不中断
            skipped += 1
            if first_err is None:
                first_err = '%s -> %r' % (s, exc)
            continue
        total += int(len(df))
        n += 1
    if skipped:
        log('[prewarm] funding skipped %d symbols (e.g. %s)' % (skipped, first_err))
    return {'symbols': n, 'rows': total, 'skipped': skipped}
