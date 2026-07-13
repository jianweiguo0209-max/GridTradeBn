"""data.binance.vision 官方归档 → ParquetCache 装载层（替代 Reservoir）。
spec: docs/superpowers/specs/2026-07-14-binance-migration-design.md §6.1

归档结构（2026-07-14 实测）：
  月度K线  {BASE_URL}/data/futures/um/monthly/klines/{SYM}/{tf}/{SYM}-{tf}-{YYYY-MM}.zip
  日度K线  {BASE_URL}/data/futures/um/daily/klines/{SYM}/{tf}/{SYM}-{tf}-{YYYY-MM-DD}.zip
  月度资金费 {BASE_URL}/data/futures/um/monthly/fundingRate/{SYM}/{SYM}-fundingRate-{YYYY-MM}.zip
  每个 zip 配 .CHECKSUM("{sha256}  {filename}")；kline CSV 12 列（老文件无表头/新文件带）；
  fundingRate CSV 带表头 calc_time,funding_interval_hours,last_funding_rate；时间戳 ms
  （防御：>1e14 视为 µs）。目录列举走 S3 XML（delimiter/prefix/marker 翻页），
  含**已退市合约**——全历史选币回放无幸存者偏差。免费无鉴权。
"""
import hashlib
import io
import os
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd

from gridtrade.exchanges.base import CANDLE_COLS, FUNDING_COLS

BASE_URL = 'https://data.binance.vision'
LIST_URL = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision'
_S3NS = '{http://s3.amazonaws.com/doc/2006-03-01/}'


def default_cache_root():
    """回测缓存根目录：BT_DATA_DIR env 覆写，默认 <repo>/data/binance。"""
    base = os.environ.get('BT_DATA_DIR')
    if base:
        return base
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '..', '..', 'data', 'binance')


def canonical_of(native, quote='USDT'):
    """'BTCUSDT' → 'BTC/USDT:USDT'；非本 quote 后缀 → None。"""
    if not native or not native.endswith(quote):
        return None
    base = native[:-len(quote)]
    if not base:
        return None
    return '%s/%s:%s' % (base, quote, quote)


def native_of(symbol):
    """'BTC/USDT:USDT' → 'BTCUSDT'。"""
    base, rest = symbol.split('/', 1)
    quote = rest.split(':')[0]
    return base + quote


def month_list(start_ms, end_ms):
    s = pd.to_datetime(start_ms, unit='ms').strftime('%Y-%m')
    e = pd.to_datetime(end_ms, unit='ms').strftime('%Y-%m')
    return [d.strftime('%Y-%m')
            for d in pd.date_range(s + '-01', e + '-01', freq='MS')]


def kline_month_url(native, tf, month):
    return ('%s/data/futures/um/monthly/klines/%s/%s/%s-%s-%s.zip'
            % (BASE_URL, native, tf, native, tf, month))


def kline_day_url(native, tf, day):
    return ('%s/data/futures/um/daily/klines/%s/%s/%s-%s-%s.zip'
            % (BASE_URL, native, tf, native, tf, day))


def funding_month_url(native, month):
    return ('%s/data/futures/um/monthly/fundingRate/%s/%s-fundingRate-%s.zip'
            % (BASE_URL, native, native, month))


def _read_zip_csv(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    return z.read(z.namelist()[0]).decode('utf-8')


def parse_kline_zip(data, symbol):
    """归档 kline zip → CANDLE_COLS df（真实 quote_volume 直取，spec §5.4）。"""
    lines = _read_zip_csv(data).splitlines()
    if lines and lines[0].startswith('open_time'):
        lines = lines[1:]
    rows = [l.split(',') for l in lines if l]
    if not rows:
        return pd.DataFrame(columns=CANDLE_COLS)
    df = pd.DataFrame(rows, columns=[
        'ts', 'open', 'high', 'low', 'close', 'vol', 'close_time',
        'quote_volume', 'count', 'tbv', 'tbqv', 'ignore'])
    df['ts'] = df['ts'].astype('int64')
    if len(df) and int(df['ts'].iloc[0]) > 10 ** 14:   # 2025+ 个别归档升微秒
        df['ts'] = df['ts'] // 1000
    for c in ('open', 'high', 'low', 'close', 'vol', 'quote_volume'):
        df[c] = df[c].astype(float)
    df['candle_begin_time'] = pd.to_datetime(df['ts'], unit='ms')
    df['symbol'] = symbol
    df['volCcy'] = df['vol']
    return df[CANDLE_COLS].sort_values('candle_begin_time').reset_index(drop=True)


def parse_funding_zip(data, symbol):
    lines = _read_zip_csv(data).splitlines()
    if lines and lines[0].startswith('calc_time'):
        lines = lines[1:]
    rows = [l.split(',') for l in lines if l]
    if not rows:
        return pd.DataFrame(columns=FUNDING_COLS)
    df = pd.DataFrame([{'ts': int(float(r[0])), 'symbol': symbol,
                        'fundingRate': float(r[2]), 'realizedRate': float(r[2])}
                       for r in rows])
    if len(df) and int(df['ts'].iloc[0]) > 10 ** 14:   # µs 防御，与 kline 同构（评审补齐）
        df['ts'] = df['ts'] // 1000
    return df[FUNDING_COLS].sort_values('ts').reset_index(drop=True)


def verify_checksum(data, checksum_text):
    toks = (checksum_text or '').split()
    if not toks:
        return False        # 空/畸形 CHECKSUM 视为校验失败（勿 IndexError）
    return hashlib.sha256(data).hexdigest() == toks[0].lower()


def _get(url, session, *, tries=3, timeout=60):
    """GET → bytes；404/耗尽 → None（调用方按'未发布'处理，不落哨兵）。"""
    import time as _t
    for i in range(tries):
        try:
            r = session.get(url, timeout=timeout)
        except Exception:
            if i < tries - 1:
                _t.sleep(1.0 + i)
            continue
        if r.status_code == 404:
            return None
        if r.status_code == 200:
            return r.content
        if i < tries - 1:
            _t.sleep(1.0 + i)
    return None


def _list_page(session, prefix, marker=None):
    url = LIST_URL + '?delimiter=/&prefix=' + prefix
    if marker:
        url += '&marker=' + marker
    data = _get(url, session)
    if data is None:
        return None
    return ET.fromstring(data.decode('utf-8'))


def list_archive_symbols(quote='USDT', *, session=None):
    """归档目录全量合约（含退市）→ canonical 列表。marker 翻页（MaxKeys 1000）。"""
    session = session or _default_session()
    prefix = 'data/futures/um/monthly/klines/'
    out, marker = [], None
    while True:
        root = _list_page(session, prefix, marker)
        if root is None:
            raise RuntimeError('data.binance.vision 目录列举失败: %s' % prefix)
        prefixes = [p.find(_S3NS + 'Prefix').text
                    for p in root.findall(_S3NS + 'CommonPrefixes')]
        for p in prefixes:
            native = p[len(prefix):].strip('/')
            sym = canonical_of(native, quote)
            if sym:
                out.append(sym)
        trunc = (root.findtext(_S3NS + 'IsTruncated') or 'false') == 'true'
        if not trunc or not prefixes:
            break
        marker = prefixes[-1]
    return sorted(set(out))


def list_available_months(native, kind, tf=None, *, session=None):
    """该合约归档已发布的月份集合（'YYYY-MM'）；列举失败 → None（调用方逐月盲试）。
    kind: 'klines'（需 tf）/ 'fundingRate'。"""
    session = session or _default_session()
    if kind == 'klines':
        prefix = 'data/futures/um/monthly/klines/%s/%s/' % (native, tf)
    else:
        prefix = 'data/futures/um/monthly/fundingRate/%s/' % native
    months, marker = set(), None
    while True:
        root = _list_page(session, prefix, marker)
        if root is None:
            return None
        keys = [c.findtext(_S3NS + 'Key') or ''
                for c in root.findall(_S3NS + 'Contents')]
        for k in keys:
            if k.endswith('.zip'):
                months.add(k[-11:-4])          # ...-YYYY-MM.zip → 'YYYY-MM'
        trunc = (root.findtext(_S3NS + 'IsTruncated') or 'false') == 'true'
        if not trunc or not keys:
            break
        marker = keys[-1]
    return months


def _default_session():
    import requests
    return requests.Session()


def _month_days(month, start_ms, end_ms, now_ms):
    """该月 ∩ 窗口、且已过完(UTC)的天列表（'YYYY-MM-DD'）。"""
    m0 = pd.Timestamp(month + '-01')
    m1 = m0 + pd.offsets.MonthBegin(1)
    lo = max(m0, pd.to_datetime(start_ms, unit='ms').normalize())
    hi = min(m1 - pd.Timedelta(days=1),
             pd.to_datetime(end_ms, unit='ms').normalize())
    if lo > hi:
        return []
    days = [d.strftime('%Y-%m-%d') for d in pd.date_range(lo, hi, freq='D')]
    return [d for d in days
            if int((pd.Timestamp(d) + pd.Timedelta(days=1)).value // 1_000_000)
            <= now_ms]


def _day_bounds_ms(day):
    d0 = pd.Timestamp(day)
    return (int(d0.value // 1_000_000),
            int((d0 + pd.Timedelta(days=1)).value // 1_000_000) - 1)


def _write_day(cache, ns, sym, day, df, cols, time_col, st):
    d_lo, d_hi = _day_bounds_ms(day)
    if df.empty:
        cache.write_empty(ns, sym, day, cols)
        st['empty_days'] += 1
        return
    ms = (df[time_col].astype('int64') if time_col == 'ts'
          else df[time_col].view('int64') // 1_000_000)
    day_df = df[(ms >= d_lo) & (ms <= d_hi)]
    if day_df.empty:
        cache.write_empty(ns, sym, day, cols)
        st['empty_days'] += 1
    else:
        cache.write(ns, sym, day, day_df.reset_index(drop=True))
        st['rows'] += int(len(day_df))


def _fetch_month(native, ns, month, session):
    """月度 zip（含尽力 CHECKSUM 校验）→ bytes / None。"""
    url = (funding_month_url(native, month) if ns == 'funding'
           else kline_month_url(native, ns, month))
    data = _get(url, session)
    if data is None:
        return None
    cs = _get(url + '.CHECKSUM', session)
    if cs is not None and not verify_checksum(data, cs.decode('utf-8', 'ignore')):
        return None
    return data


_UNSET = object()


def _warm_symbol(cache, sym, ns, months, start_ms, end_ms, now_ms, session, log):
    native = native_of(sym)
    kind = 'fundingRate' if ns == 'funding' else 'klines'
    avail = _UNSET   # 惰性加载：整窗全命中缓存时零 HTTP（幂等重跑不浪费 530×ns 次列举）
    parse = parse_funding_zip if ns == 'funding' else parse_kline_zip
    cols = FUNDING_COLS if ns == 'funding' else CANDLE_COLS
    time_col = 'ts' if ns == 'funding' else 'candle_begin_time'
    st = {'rows': 0, 'files': 0, 'skipped_cached': 0, 'retry_later': 0,
          'empty_days': 0}
    for month in months:
        days = _month_days(month, start_ms, end_ms, now_ms)
        if not days:                      # 当月全部天未过完 → 下次重取
            st['retry_later'] += 1
            continue
        missing = [d for d in days if not cache.exists(ns, sym, d)]
        if not missing:
            st['skipped_cached'] += 1
            continue
        if avail is _UNSET:
            avail = list_available_months(native, kind,
                                          tf=None if ns == 'funding' else ns,
                                          session=session)
        if avail is not None and month not in avail:
            if avail and month < min(avail):
                # 上市前月份：真·无数据 → 空哨兵（不再反复重试）
                for d in missing:
                    cache.write_empty(ns, sym, d, cols)
                    st['empty_days'] += 1
                continue
            # 月度未发布（近月）：kline 日度回退；funding 无日度 → 尾部交 API 补
            if ns == 'funding':
                st['retry_later'] += 1
                continue
            for d in missing:
                data = _get(kline_day_url(native, ns, d), session)
                if data is None:
                    st['retry_later'] += 1
                    continue
                st['files'] += 1
                _write_day(cache, ns, sym, d, parse(data, sym), cols, time_col, st)
            continue
        data = _fetch_month(native, ns, month, session)
        if data is None:                  # 404/校验不符 → 不落哨兵，下次重取
            st['retry_later'] += 1
            continue
        st['files'] += 1
        df = parse(data, sym)
        for d in missing:
            _write_day(cache, ns, sym, d, df, cols, time_col, st)
    return st


def warm_vision(cache, universe, start_ms, end_ms, *, timeframes=('1m',),
                quote='USDT', workers=None, session=None, log=print):
    """把窗口内归档数据写入 cache 各命名空间（'1m'/'1h'/'funding'）。幂等：
    整月全命中即跳过；失败/未发布不落哨兵（retry_later）。线程池按 (ns,symbol)
    并行（BT_VISION_WORKERS，默认 8）。返回 stats（形状见测试）。"""
    from concurrent.futures import ThreadPoolExecutor
    sess = session or _default_session()
    now_ms = int(pd.Timestamp.utcnow().value // 1_000_000)
    months = month_list(start_ms, end_ms)
    nworkers = int(workers if workers is not None
                   else os.environ.get('BT_VISION_WORKERS', '8'))
    stats = {ns: {'rows': 0, 'files': 0} for ns in timeframes}
    stats.update({'skipped_cached': 0, 'retry_later': 0, 'empty_days': 0})
    units = [(ns, s) for ns in timeframes for s in universe]

    def run(unit):
        ns, s = unit
        return ns, _warm_symbol(cache, s, ns, months, start_ms, end_ms,
                                now_ms, sess, log)

    if nworkers > 1 and len(units) > 1:
        with ThreadPoolExecutor(max_workers=nworkers) as ex:
            results = list(ex.map(run, units))
    else:
        results = [run(u) for u in units]
    done = 0
    for ns, st in results:
        stats[ns]['rows'] += st['rows']
        stats[ns]['files'] += st['files']
        for k in ('skipped_cached', 'retry_later', 'empty_days'):
            stats[k] += st[k]
        done += 1
        if done % 50 == 0:
            log('[vision] %d/%d units done' % (done, len(units)))
    return stats
