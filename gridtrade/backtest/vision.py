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
