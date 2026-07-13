"""vision 归档装载层单测——全离线：zip 在内存现造，HTTP 经注入桩。"""
import hashlib
import io
import zipfile

import pandas as pd


def _zip_bytes(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(name, text)
    return buf.getvalue()


KLINE_NOHDR = ("1577836800000,1.0,2.0,0.5,1.5,10.0,1577836859999,13.7,5,4.0,5.5,0\n"
               "1577836860000,1.5,2.5,1.0,2.0,20.0,1577836919999,36.2,8,9.0,16.3,0\n")
KLINE_HDR = ("open_time,open,high,low,close,volume,close_time,quote_volume,"
             "count,taker_buy_volume,taker_buy_quote_volume,ignore\n" + KLINE_NOHDR)
FUNDING_CSV = ("calc_time,funding_interval_hours,last_funding_rate\n"
               "1577836800000,8,-0.00012359\n"
               "1577865600000,8,0.00030000\n")


def test_symbol_mapping_roundtrip():
    from gridtrade.backtest import vision as V
    assert V.canonical_of('BTCUSDT') == 'BTC/USDT:USDT'
    assert V.canonical_of('1000BONKUSDC') is None          # 非本 quote → None
    assert V.canonical_of('BTCUSDT', quote='USDT') == V.canonical_of('BTCUSDT')
    assert V.native_of('BTC/USDT:USDT') == 'BTCUSDT'


def test_month_list_and_urls():
    from gridtrade.backtest import vision as V
    ms = lambda s: int(pd.Timestamp(s).value // 1_000_000)
    assert V.month_list(ms('2019-12-15'), ms('2020-02-01')) == \
        ['2019-12', '2020-01', '2020-02']
    assert V.kline_month_url('BTCUSDT', '1m', '2020-01') == \
        ('https://data.binance.vision/data/futures/um/monthly/klines/'
         'BTCUSDT/1m/BTCUSDT-1m-2020-01.zip')
    assert V.kline_day_url('BTCUSDT', '1h', '2020-01-02') == \
        ('https://data.binance.vision/data/futures/um/daily/klines/'
         'BTCUSDT/1h/BTCUSDT-1h-2020-01-02.zip')
    assert V.funding_month_url('BTCUSDT', '2020-01') == \
        ('https://data.binance.vision/data/futures/um/monthly/fundingRate/'
         'BTCUSDT/BTCUSDT-fundingRate-2020-01.zip')


def test_parse_kline_zip_with_and_without_header():
    from gridtrade.backtest import vision as V
    from gridtrade.exchanges.base import CANDLE_COLS
    for text in (KLINE_NOHDR, KLINE_HDR):
        df = V.parse_kline_zip(_zip_bytes('x.csv', text), 'BTC/USDT:USDT')
        assert list(df.columns) == CANDLE_COLS
        assert df['quote_volume'].tolist() == [13.7, 36.2]   # 真实报价成交额
        assert df['volCcy'].tolist() == [10.0, 20.0]
        assert df['candle_begin_time'].iloc[0] == pd.Timestamp('2020-01-01 00:00:00')


def test_parse_kline_zip_microsecond_defense():
    from gridtrade.backtest import vision as V
    text = "1577836800000000,1.0,2.0,0.5,1.5,10.0,1577836859999999,13.7,5,4.0,5.5,0\n"
    df = V.parse_kline_zip(_zip_bytes('x.csv', text), 'BTC/USDT:USDT')
    assert df['candle_begin_time'].iloc[0] == pd.Timestamp('2020-01-01 00:00:00')


def test_parse_funding_zip():
    from gridtrade.backtest import vision as V
    from gridtrade.exchanges.base import FUNDING_COLS
    df = V.parse_funding_zip(_zip_bytes('f.csv', FUNDING_CSV), 'BTC/USDT:USDT')
    assert list(df.columns) == FUNDING_COLS
    assert df['fundingRate'].tolist() == [-0.00012359, 0.0003]
    assert df['realizedRate'].tolist() == df['fundingRate'].tolist()
    assert df['ts'].tolist() == [1577836800000, 1577865600000]


def test_parse_funding_zip_microsecond_defense():
    from gridtrade.backtest import vision as V
    text = ("calc_time,funding_interval_hours,last_funding_rate\n"
            "1577836800000000,8,-0.0001\n")
    df = V.parse_funding_zip(_zip_bytes('f.csv', text), 'BTC/USDT:USDT')
    assert df['ts'].tolist() == [1577836800000]


def test_verify_checksum():
    from gridtrade.backtest import vision as V
    data = b'hello'
    good = hashlib.sha256(data).hexdigest() + '  file.zip'
    assert V.verify_checksum(data, good)
    assert not V.verify_checksum(data, 'deadbeef  file.zip')


def test_verify_checksum_malformed_returns_false():
    from gridtrade.backtest import vision as V
    assert not V.verify_checksum(b'x', '')
    assert not V.verify_checksum(b'x', None)
    assert not V.verify_checksum(b'x', '   ')


class _FakeResp:
    def __init__(self, status, content=b''):
        self.status_code = status
        self.content = content


class _FakeSession:
    """按 URL 查表的 requests.Session 桩。"""
    def __init__(self, table):
        self.table = table
        self.calls = []
    def get(self, url, timeout=None):
        self.calls.append(url)
        v = self.table.get(url)
        if v is None:
            return _FakeResp(404)
        return _FakeResp(200, v)


LIST_XML_P1 = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<IsTruncated>true</IsTruncated>
<CommonPrefixes><Prefix>data/futures/um/monthly/klines/BTCUSDT/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>data/futures/um/monthly/klines/1000BONKUSDC/</Prefix></CommonPrefixes>
</ListBucketResult>"""
LIST_XML_P2 = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<IsTruncated>false</IsTruncated>
<CommonPrefixes><Prefix>data/futures/um/monthly/klines/ETHUSDT/</Prefix></CommonPrefixes>
</ListBucketResult>"""


def test_list_archive_symbols_paginates_and_filters():
    from gridtrade.backtest import vision as V
    base = V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/klines/'
    sess = _FakeSession({
        base: LIST_XML_P1.encode(),
        base + '&marker=data/futures/um/monthly/klines/1000BONKUSDC/':
            LIST_XML_P2.encode(),
    })
    syms = V.list_archive_symbols(session=sess)
    assert syms == ['BTC/USDT:USDT', 'ETH/USDT:USDT']   # USDC 目录被 quote 过滤


MONTHS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<IsTruncated>false</IsTruncated>
<Contents><Key>data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2020-01.zip</Key></Contents>
<Contents><Key>data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2020-01.zip.CHECKSUM</Key></Contents>
<Contents><Key>data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2020-02.zip</Key></Contents>
</ListBucketResult>"""


def test_list_available_months():
    from gridtrade.backtest import vision as V
    url = (V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/klines/'
           'BTCUSDT/1m/')
    sess = _FakeSession({url: MONTHS_XML.encode()})
    assert V.list_available_months('BTCUSDT', 'klines', tf='1m',
                                   session=sess) == {'2020-01', '2020-02'}
    assert V.list_available_months('BTCUSDT', 'klines', tf='1m',
                                   session=_FakeSession({})) is None


def _cache(tmp_path):
    from gridtrade.backtest.cache import ParquetCache
    return ParquetCache(str(tmp_path))


def _month_zip_urls(native, tf, month):
    from gridtrade.backtest import vision as V
    u = V.kline_month_url(native, tf, month)
    return u, u + '.CHECKSUM'


def test_warm_vision_writes_days_and_idempotent(tmp_path):
    from gridtrade.backtest import vision as V
    # 2020-01 两根 1m（1/1 与 1/2 各一根）→ 两天各 1 行，其余窗口天=空哨兵
    csv = ("1577836800000,1.0,2.0,0.5,1.5,10.0,1577836859999,13.7,5,4.0,5.5,0\n"
           "1577923200000,1.5,2.5,1.0,2.0,20.0,1577923259999,36.2,8,9.0,16.3,0\n")
    data = _zip_bytes('x.csv', csv)
    import hashlib as _h
    u, cs_u = _month_zip_urls('BTCUSDT', '1m', '2020-01')
    months_url = (V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/'
                  'klines/BTCUSDT/1m/')
    sess = _FakeSession({
        u: data,
        cs_u: (_h.sha256(data).hexdigest() + '  x.zip').encode(),
        months_url: MONTHS_XML.encode(),          # 可用月 {2020-01, 2020-02}
    })
    cache = _cache(tmp_path)
    ms = lambda s: int(pd.Timestamp(s).value // 1_000_000)
    st = V.warm_vision(cache, ['BTC/USDT:USDT'], ms('2020-01-01'), ms('2020-01-03'),
                       timeframes=('1m',), workers=1, session=sess)
    assert st['1m']['rows'] == 2 and st['1m']['files'] == 1
    assert cache.read('1m', 'BTC/USDT:USDT', '2020-01-01')['close'].tolist() == [1.5]
    assert cache.read('1m', 'BTC/USDT:USDT', '2020-01-02')['close'].tolist() == [2.0]
    empty = cache.read('1m', 'BTC/USDT:USDT', '2020-01-03')
    assert empty is not None and empty.empty          # 月内无数据天=空哨兵
    # 幂等：第二遍全命中，零下载
    n_calls = len(sess.calls)
    st2 = V.warm_vision(cache, ['BTC/USDT:USDT'], ms('2020-01-01'), ms('2020-01-03'),
                        timeframes=('1m',), workers=1, session=sess)
    assert st2['skipped_cached'] == 1 and len(sess.calls) == n_calls


def test_warm_vision_prelisting_month_empty_sentinel(tmp_path):
    from gridtrade.backtest import vision as V
    months_url = (V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/'
                  'klines/BTCUSDT/1m/')
    sess = _FakeSession({months_url: MONTHS_XML.encode()})   # 可用月起点 2020-01
    cache = _cache(tmp_path)
    ms = lambda s: int(pd.Timestamp(s).value // 1_000_000)
    st = V.warm_vision(cache, ['BTC/USDT:USDT'], ms('2019-12-30'), ms('2019-12-31'),
                       timeframes=('1m',), workers=1, session=sess)
    assert st['empty_days'] == 2                       # 上市前=真·无数据
    assert cache.exists('1m', 'BTC/USDT:USDT', '2019-12-30')


def test_warm_vision_missing_month_daily_fallback(tmp_path):
    from gridtrade.backtest import vision as V
    # 月度缺且窗口月 > 首个可用月（近月未发布），走日度：1/1 有文件、1/2 404 → retry_later
    csv = "1577836800000,1.0,2.0,0.5,1.5,10.0,1577836859999,13.7,5,4.0,5.5,0\n"
    months_url = (V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/'
                  'klines/BTCUSDT/1m/')
    # 可用月={'2019-12'}：目标月 2020-01 不在其中、且不小于 min(avail) → 日度回退分支
    xml_only_dec = MONTHS_XML.replace('2020-01', '2019-12').replace('2020-02', '2019-12')
    sess = _FakeSession({
        months_url: xml_only_dec.encode(),
        V.kline_day_url('BTCUSDT', '1m', '2020-01-01'): _zip_bytes('d.csv', csv),
    })
    cache = _cache(tmp_path)
    ms = lambda s: int(pd.Timestamp(s).value // 1_000_000)
    st = V.warm_vision(cache, ['BTC/USDT:USDT'], ms('2020-01-01'), ms('2020-01-02'),
                       timeframes=('1m',), workers=1, session=sess)
    assert st['1m']['rows'] == 1
    assert st['retry_later'] == 1                      # 1/2 未发布，下次重取
    assert not cache.exists('1m', 'BTC/USDT:USDT', '2020-01-02')


def test_warm_vision_funding_namespace(tmp_path):
    from gridtrade.backtest import vision as V
    data = _zip_bytes('f.csv', FUNDING_CSV)
    months_url = (V.LIST_URL + '?delimiter=/&prefix=data/futures/um/monthly/'
                  'fundingRate/BTCUSDT/')
    xml = MONTHS_XML.replace('klines/BTCUSDT/1m/BTCUSDT-1m', 'fundingRate/BTCUSDT/BTCUSDT-fundingRate')
    sess = _FakeSession({
        months_url: xml.encode(),
        V.funding_month_url('BTCUSDT', '2020-01'): data,
        V.funding_month_url('BTCUSDT', '2020-01') + '.CHECKSUM': None and b'',
    })
    cache = _cache(tmp_path)
    ms = lambda s: int(pd.Timestamp(s).value // 1_000_000)
    st = V.warm_vision(cache, ['BTC/USDT:USDT'], ms('2020-01-01'), ms('2020-01-01'),
                       timeframes=('funding',), workers=1, session=sess)
    # 两条记录 ts=00:00 与 08:00 同属 2020-01-01（8h 资金费一天多条）
    assert st['funding']['rows'] == 2
    got = cache.read('funding', 'BTC/USDT:USDT', '2020-01-01')
    assert got['fundingRate'].tolist() == [-0.00012359, 0.0003]
