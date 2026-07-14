"""选币结果磁盘缓存：key = 选币参数 + 每币**窗口范围内**缓存天数据指纹；命中即秒回，跳过整段选币计算。
仅回测离线工具用。version+params 双校验防 sha256 碰撞；BT_SELECT_CACHE=off 旁路。
指纹只覆盖 [window_start−回看, window_end]（选币对每 run_time 只读 <run_time 的最近 max_candle_num
根），故**追加 window_end 之后的近期天 / 回填回看之前的远古天都不翻 key**（对该窗选币无影响）；
只有窗口相关那段天变化才失效。数据指纹用 ParquetCache.list_days（廉价 listdir），
「就地改写某天旧文件内容」这类指纹仍盖不住（靠 CACHE_VERSION bump / BT_SELECT_CACHE=off 兜底）。"""
import hashlib
import json
import os
import pickle
import tempfile

import pandas as pd

CACHE_VERSION = 2
_NAMESPACE = '_select_cache'


def enabled():
    return os.environ.get('BT_SELECT_CACHE', 'on').lower() != 'off'


def _window_day_bounds(window_start, window_end, timeframe, max_candle_num):
    """选币相关天范围 [lo_day, hi_day]（'YYYY-MM-DD'，ISO 字典序=时序）。
    下界取足回看 + 2 天缓冲：宁可多含（多失效一点，安全）也不少含（漏检=返回过期，不可接受）。"""
    lo = pd.Timestamp(window_start) - pd.Timedelta(timeframe) * int(max_candle_num) \
        - pd.Timedelta(days=2)
    return lo.strftime('%Y-%m-%d'), pd.Timestamp(window_end).strftime('%Y-%m-%d')


def _fingerprint(cache, universe, timeframe, lo_day, hi_day):
    """每个 symbol 落在 [lo_day, hi_day] 内的缓存天列表；范围外的天不进指纹。无相关天→None。"""
    fp = {}
    for s in sorted(universe):
        days = [d for d in cache.list_days(timeframe, s) if lo_day <= d <= hi_day]
        fp[s] = days if days else None
    return fp


def compute_key(cache, universe, window_start, window_end, timeframe,
                min_quote_volume, blacklist, strategy_config, factors,
                top_volume_pct=0.0):
    """返回 (key_hex16, params_dict)。params 含窗口范围内数据指纹，改变相关天时自动换 key；
    追加 window_end 之后的近期天不换 key。"""
    lo_day, hi_day = _window_day_bounds(window_start, window_end, timeframe,
                                        strategy_config['max_candle_num'])
    params = {
        'version': CACHE_VERSION,
        'window_start': str(window_start),
        'window_end': str(window_end),
        'timeframe': timeframe,
        'universe': sorted(universe),
        'blacklist': sorted(blacklist),
        'min_quote_volume': float(min_quote_volume),
        'top_volume_pct': float(top_volume_pct),   # 相对口径入 key：不同 pct 不串缓存
        'period': strategy_config['period'],
        'weight_list': list(strategy_config['weight_list']),
        'choose_symbols': strategy_config['choose_symbols'],
        'max_candle_num': strategy_config['max_candle_num'],
        'factors': {k: bool(v) for k, v in factors.items()},
        'fingerprint': _fingerprint(cache, universe, timeframe, lo_day, hi_day),
    }
    blob = json.dumps(params, sort_keys=True, default=str)
    key = hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]
    return key, params


def _dir(cache):
    return os.path.join(cache.root, _NAMESPACE)


def _path(cache, key):
    return os.path.join(_dir(cache), '%s.pkl' % key)


def load(cache, key, params):
    """命中且 version+params 完全一致 → 返回 grids；否则 None。"""
    p = _path(cache, key)
    if not (os.path.exists(p) and os.path.getsize(p) > 0):
        return None
    try:
        with open(p, 'rb') as f:
            obj = pickle.load(f)
    except BaseException:
        return None
    if obj.get('version') != CACHE_VERSION or obj.get('params') != params:
        return None                       # 防 sha256 碰撞 / 版本漂移
    return obj.get('grids')


def save(cache, key, params, grids):
    """原子写 pkl（临时文件 + os.replace）。"""
    d = _dir(cache)
    os.makedirs(d, exist_ok=True)
    p = _path(cache, key)
    fd, tmp = tempfile.mkstemp(dir=d, suffix='.tmp')
    os.close(fd)
    try:
        with open(tmp, 'wb') as f:
            pickle.dump({'version': CACHE_VERSION, 'params': params, 'grids': grids}, f)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
